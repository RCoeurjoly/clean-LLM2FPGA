// Share graph-derived private functions only after matching their complete
// ordered SSA/control structure. Different scalar constants become arguments;
// private rank-one storage may grow only when no operation observes capacity.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
std::optional<Type> capacity(Type a, Type b) {
  if (a == b) return a;
  auto x = dyn_cast<MemRefType>(a), y = dyn_cast<MemRefType>(b);
  if (!x || !y || x.getRank() != 1 || y.getRank() != 1 ||
      !x.hasStaticShape() || !y.hasStaticShape() ||
      x.getDimSize(0) <= 0 || y.getDimSize(0) <= 0 ||
      !x.getLayout().isIdentity() || !y.getLayout().isIdentity() ||
      x.getElementType() != y.getElementType() ||
      x.getMemorySpace() != y.getMemorySpace()) return std::nullopt;
  return MemRefType::get({std::max(x.getDimSize(0), y.getDimSize(0))},
                        x.getElementType(), MemRefLayoutAttrInterface{}, x.getMemorySpace());
}

bool addressOnly(Value buffer) {
  return llvm::all_of(buffer.getUses(), [&](OpOperand &use) {
    auto owner = use.getOwner();
    if (auto load = dyn_cast<memref::LoadOp>(owner))
      return load.getMemRef() == buffer;
    if (auto store = dyn_cast<memref::StoreOp>(owner))
      return store.getMemRef() == buffer;
    return false;
  });
}

// Only unannotated scalar integer constants are interchangeable at their use
// sites. Other operations and annotations retain the exact SSA comparison.
arith::ConstantOp plainIntegerConstant(Operation *op) {
  auto constant = dyn_cast_or_null<arith::ConstantOp>(op);
  return constant && op->getAttrs().size() == 1 &&
                 isa<IntegerAttr>(constant.getValue())
             ? constant : arith::ConstantOp{};
}

void operations(Block &block, SmallVectorImpl<Operation *> &result) {
  for (auto &op : block) {
    if (!plainIntegerConstant(&op)) result.push_back(&op);
    for (auto &region : op.getRegions())
      for (auto &child : region) operations(child, result);
  }
}

bool attributesEqual(Operation *a, Operation *b, bool constant) {
  NamedAttrList left(a->getAttrs()), right(b->getAttrs());
  if (isa<memref::LoadOp>(a) && isa<memref::LoadOp>(b)) {
    // Alias folding may materialize the builtin false property on only one
    // load. Its absence has the same default semantics; true hints and all
    // other annotations still participate in the exact comparison.
    for (auto *attributes : {&left, &right})
      if (auto hint = dyn_cast_or_null<BoolAttr>(attributes->get("nontemporal")))
        if (!hint.getValue()) attributes->erase("nontemporal");
  }
  if (left.getAttrs().size() != right.getAttrs().size()) return false;
  for (auto attribute : left) {
    if (constant && attribute.getName() == "value") continue;
    if (right.get(attribute.getName()) != attribute.getValue()) return false;
  }
  return true;
}

// Function annotations that describe a captured specialization are retained in
// shared_variants. All other annotations, including argument contracts, match.
bool metadataEqual(func::FuncOp a, func::FuncOp b) {
  auto ignored = [](StringRef name) {
    return name == "sym_name" || name == "function_type" ||
           name == "llm2fpga.context_length" ||
           name == "llm2fpga.input_manifest_sha256";
  };
  for (auto attribute : a->getAttrs())
    if (!ignored(attribute.getName().strref()) &&
        b->getAttr(attribute.getName()) != attribute.getValue()) return false;
  for (auto attribute : b->getAttrs())
    if (!ignored(attribute.getName().strref()) &&
        a->getAttr(attribute.getName()) != attribute.getValue()) return false;
  return true;
}

bool sameBlock(Block &a, Block &b, llvm::DenseMap<Value, Value> &mapped) {
  if (a.getNumArguments() != b.getNumArguments()) return false;
  for (auto [x, y] : llvm::zip(a.getArguments(), b.getArguments())) {
    if (!capacity(x.getType(), y.getType())) return false;
    if (x.getType() != y.getType() && (!addressOnly(x) || !addressOnly(y))) return false;
    mapped[x] = y;
  }
  SmallVector<Operation *> left, right;
  for (auto &op : a) if (!plainIntegerConstant(&op)) left.push_back(&op);
  for (auto &op : b) if (!plainIntegerConstant(&op)) right.push_back(&op);
  if (left.size() != right.size()) return false;
  for (auto [xp, yp] : llvm::zip(left, right)) {
    auto &x = *xp;
    auto &y = *yp;
    if (x.getName() != y.getName() || x.getNumOperands() != y.getNumOperands() ||
        x.getNumResults() != y.getNumResults() || x.getNumRegions() != y.getNumRegions() ||
        x.getNumSuccessors() || y.getNumSuccessors()) return false;
    // Calls, aliases and capacity observers are outside this proof. No unknown
    // operation may manipulate a buffer whose representation could change.
    if (isa<func::CallOp>(x)) return false;
    for (auto [u, v] : llvm::zip(x.getOperands(), y.getOperands())) {
      auto uc = plainIntegerConstant(u.getDefiningOp());
      auto vc = plainIntegerConstant(v.getDefiningOp());
      if (uc || vc) {
        if (!uc || !vc || u.getType() != v.getType()) return false;
      } else if (mapped.lookup(u) != v) return false;
      if (isa<MemRefType>(u.getType()) && !isa<memref::LoadOp, memref::StoreOp>(x)) return false;
    }
    if (!attributesEqual(&x, &y, false)) return false;
    for (auto [u, v] : llvm::zip(x.getResults(), y.getResults())) {
      if (u.getType() != v.getType()) {
        if (!isa<memref::AllocOp>(x) || !capacity(u.getType(), v.getType()) ||
            !addressOnly(u) || !addressOnly(v)) return false;
      }
      mapped[u] = v;
    }
    for (auto [r, s] : llvm::zip(x.getRegions(), y.getRegions())) {
      if (r.getBlocks().size() != s.getBlocks().size()) return false;
      for (auto [u, v] : llvm::zip(r, s)) if (!sameBlock(u, v, mapped)) return false;
    }
  }
  return true;
}

bool equivalent(func::FuncOp a, func::FuncOp b) {
  if (a.getNumResults() || b.getNumResults() || !metadataEqual(a, b)) return false;
  llvm::DenseMap<Value, Value> mapped;
  return sameBlock(a.getBody().front(), b.getBody().front(), mapped);
}

struct FunctionSharing : PassWrapper<FunctionSharing, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(FunctionSharing)
  FunctionSharing() = default;
  FunctionSharing(const FunctionSharing &other) : PassWrapper(other) {}
  Option<std::string> entrypoint{*this, "entrypoint", llvm::cl::init("main")};
  StringRef getArgument() const final { return "llm2fpga-share-private-functions"; }
  StringRef getDescription() const final { return "Share proven private SSA specializations through scalar parameters"; }
  void runOnOperation() override {
    auto module = getOperation();
    auto root = module.lookupSymbol<func::FuncOp>(entrypoint);
    if (!root || root.isExternal() || !llvm::hasSingleElement(root.getBody())) return;
    auto &callerBlock = root.getBody().front();
    SmallVector<func::FuncOp> helpers;
    for (auto call : callerBlock.getOps<func::CallOp>()) {
      auto helper = module.lookupSymbol<func::FuncOp>(call.getCallee());
      if (!helper || helper == root || !helper.isPrivate() || helper.isExternal() ||
          !llvm::hasSingleElement(helper.getBody()) || llvm::is_contained(helpers, helper)) continue;
      auto uses = SymbolTable::getSymbolUses(helper, module);
      if (!uses || llvm::any_of(*uses, [&](const SymbolTable::SymbolUse &use) {
        auto caller = dyn_cast<func::CallOp>(use.getUser());
        return !caller || caller->getBlock() != &callerBlock;
      })) continue;
      helpers.push_back(helper);
    }
    llvm::SmallPtrSet<Operation *, 16> handled;
    for (auto base : helpers) {
      if (handled.contains(base)) continue;
      SmallVector<func::FuncOp> group{base};
      for (auto other : helpers)
        if (other != base && !handled.contains(other) && equivalent(base, other)) group.push_back(other);
      if (group.size() < 2) continue;
      SmallVector<SmallVector<Operation *>> bodies(group.size());
      for (auto [i, function] : llvm::enumerate(group)) operations(function.getBody().front(), bodies[i]);
      SmallVector<Type> argumentTypes(base.getArgumentTypes());
      for (auto function : group)
        for (unsigned i = 0; i < argumentTypes.size(); ++i)
          argumentTypes[i] = *capacity(argumentTypes[i], function.getArgument(i).getType());
      // A caller allocation may grow only if every use is an indexed access or
      // one of these exclusive calls, with the same requested capacity.
      llvm::DenseMap<Value, Type> promotions;
      bool safe = true;
      SmallVector<func::CallOp> calls;
      for (auto call : callerBlock.getOps<func::CallOp>()) {
        auto function = module.lookupSymbol<func::FuncOp>(call.getCallee());
        if (!llvm::is_contained(group, function)) continue;
        calls.push_back(call);
        for (auto [i, value] : llvm::enumerate(call.getOperands())) {
          Type target = argumentTypes[i];
          auto old = promotions.find(value);
          if (old != promotions.end() && old->second != target) safe = false;
          promotions[value] = target;
          if (value.getType() == target) continue;
          auto allocation = value.getDefiningOp<memref::AllocOp>();
          if (!allocation || allocation->getBlock() != &callerBlock || allocation.getNumOperands()) { safe = false; continue; }
          for (auto &use : value.getUses()) {
            auto owner = use.getOwner();
            if (isa<memref::LoadOp, memref::StoreOp>(owner)) continue;
            auto user = dyn_cast<func::CallOp>(owner);
            if (!user || user->getBlock() != &callerBlock ||
                !llvm::is_contained(group, module.lookupSymbol<func::FuncOp>(user.getCallee())) ||
                argumentTypes[use.getOperandNumber()] != target) safe = false;
          }
        }
      }
      if (!safe) continue;
      struct ScalarParameter {
        Type type;
        SmallVector<TypedAttr> values;
        SmallVector<OpOperand *> uses;
      };
      SmallVector<ScalarParameter> parameters;
      for (unsigned i = 0; i < bodies[0].size(); ++i)
        for (auto &use : bodies[0][i]->getOpOperands()) {
          auto constant = plainIntegerConstant(use.get().getDefiningOp());
          if (!constant) continue;
          SmallVector<TypedAttr> values;
          for (auto &body : bodies)
            values.push_back(plainIntegerConstant(
                body[i]->getOperand(use.getOperandNumber()).getDefiningOp()).getValue());
          if (llvm::all_of(values, [&](Attribute value) { return value == values[0]; })) continue;
          auto found = llvm::find_if(parameters, [&](const ScalarParameter &parameter) {
            return parameter.type == constant.getType() && parameter.values == values;
          });
          if (found == parameters.end())
            parameters.push_back({constant.getType(), std::move(values), {&use}});
          else found->uses.push_back(&use);
        }
      OpBuilder builder(&getContext());
      SmallVector<Attribute> variants;
      for (auto function : group) variants.push_back(function->getAttrDictionary());
      base->setAttr("llm2fpga.shared_variants", builder.getArrayAttr(variants));
      for (auto [value, type] : promotions) value.setType(type);
      for (unsigned i = 0; i < argumentTypes.size(); ++i) base.getArgument(i).setType(argumentTypes[i]);
      for (unsigned i = 0; i < bodies[0].size(); ++i) {
        auto allocation = dyn_cast<memref::AllocOp>(bodies[0][i]);
        if (!allocation) continue;
        Type type = allocation.getType();
        for (auto &body : bodies) type = *capacity(type, body[i]->getResult(0).getType());
        allocation.getResult().setType(cast<MemRefType>(type));
      }
      base.setType(FunctionType::get(&getContext(), argumentTypes, {}));
      auto originalArguments = argumentTypes.size();
      for (const auto &parameter : parameters) {
        if (failed(base.insertArgument(base.getNumArguments(), parameter.type, DictionaryAttr{}, base.getLoc()))) {
          signalPassFailure(); return;
        }
      }
      for (auto call : calls) {
        auto function = module.lookupSymbol<func::FuncOp>(call.getCallee());
        unsigned variant = llvm::find(group, function) - group.begin();
        builder.setInsertionPoint(call);
        for (const auto &parameter : parameters) {
          auto argument = builder.create<arith::ConstantOp>(call.getLoc(), parameter.type, parameter.values[variant]);
          call.getOperandsMutable().append(argument.getResult());
        }
        call.setCallee(base.getSymName());
      }
      // One pooled base constant may correspond to different values at separate
      // use sites. Replace each proved use, never all uses of its definition.
      for (auto [i, parameter] : llvm::enumerate(parameters))
        for (auto *use : parameter.uses) use->set(base.getArgument(originalArguments + i));
      SmallVector<Operation *> deadConstants;
      base.walk([&](arith::ConstantOp constant) {
        if (plainIntegerConstant(constant) && constant->use_empty()) deadConstants.push_back(constant);
      });
      for (auto *constant : deadConstants) constant->erase();
      base.setType(FunctionType::get(&getContext(), base.getBody().front().getArgumentTypes(), {}));
      for (auto function : group) {
        handled.insert(function);
        if (function != base) function.erase();
      }
    }
  }
};
}
void llm2fpga::registerFunctionSharing() { PassRegistration<FunctionSharing>(); }
