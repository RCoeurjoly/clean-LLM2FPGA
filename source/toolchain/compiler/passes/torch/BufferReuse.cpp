// Reuse private scratch buffers only after proving full initialization and
// disjoint structured lifetimes. Function arguments and aliases are excluded.
#include "TypedTensor.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include <limits>
using namespace mlir;
namespace {
Operation *rootInBlock(Operation *operation, Block *block) {
  while (operation && operation->getBlock() != block)
    operation = operation->getParentOp();
  return operation;
}

bool fullWrite(Operation *operation, Value buffer, Block *block) {
  // Reject captured accesses in a Linalg region or nested control. The first
  // operation must directly write every element before any read of old data.
  for (OpOperand &use : buffer.getUses())
    if (rootInBlock(use.getOwner(), block) == operation &&
        use.getOwner() != operation)
      return false;
  if (auto copy = dyn_cast<memref::CopyOp>(operation))
    return copy.getTarget() == buffer && copy.getSource() != buffer;
  auto generic = dyn_cast<linalg::GenericOp>(operation);
  if (auto fill = dyn_cast<linalg::FillOp>(operation))
    return fill.getOutputs().size() == 1 && fill.getOutputs()[0] == buffer;
  if (!generic || !generic.hasPureBufferSemantics()) return false;
  for (Value input : generic.getInputs())
    if (input == buffer) return false;
  OpOperand *output = nullptr;
  for (OpOperand &operand : generic->getOpOperands())
    if (operand.get() == buffer) {
      if (output || !generic.isDpsInit(&operand)) return false;
      output = &operand;
    }
  if (!output) return false;
  if (!generic.getBlock()->getArgument(output->getOperandNumber()).use_empty())
    return false;
  auto type = cast<MemRefType>(buffer.getType());
  auto map = generic.getMatchingIndexingMap(output);
  if (!generic.getShapesToLoopsMap()) return false;
  auto ranges = generic.getStaticLoopRanges();
  if (map.getNumResults() != type.getRank() ||
      llvm::any_of(ranges, [](int64_t size) { return size <= 0; }))
    return false;
  llvm::SmallDenseSet<unsigned> dimensions;
  for (auto [dimension, expression] : llvm::enumerate(map.getResults())) {
    if (auto index = dyn_cast<AffineDimExpr>(expression)) {
      auto position = index.getPosition();
      if (!dimensions.insert(position).second || position >= ranges.size() ||
          ranges[position] != type.getDimSize(dimension)) return false;
    } else if (auto constant = dyn_cast<AffineConstantExpr>(expression)) {
      if (constant.getValue() != 0 || type.getDimSize(dimension) != 1)
        return false;
    } else return false;
  }
  return true;
}

std::optional<int64_t> checkedElements(MemRefType type) {
  if (!type.hasStaticShape()) return std::nullopt;
  APInt count(64,1);
  for (int64_t n:type.getShape()) {
    if (n<=0) return std::nullopt;
    bool overflow=false;
    count=count.smul_ov(APInt(64,n),overflow);
    if (overflow || count.isNegative()) return std::nullopt;
  }
  return count.getSExtValue();
}

struct Candidate {
  memref::AllocOp allocation;
  unsigned first, last;
};

SmallVector<Candidate> collectCandidates(Block &block, bool trackOwnedReshapes = false) {
      llvm::DenseMap<Operation *, unsigned> order;
      for (auto [index, operation] : llvm::enumerate(block))
        order[&operation] = index;
      SmallVector<Candidate> candidates;
      for (auto allocation : block.getOps<memref::AllocOp>()) {
        auto type = allocation.getType();
        if (!type.hasStaticShape() ||
            llvm::any_of(type.getShape(), [](int64_t size) { return size <= 0; }) ||
            allocation.getNumOperands() != 0 ||
            allocation.getResult().use_empty()) continue;
        // Native operand-segment properties are not semantic annotations.
        // Exclude all extra annotations, including provenance or bindings.
        if (!allocation->getDiscardableAttrDictionary().empty()) continue;
        unsigned first = order.size(), firstData = order.size(), last = 0;
        Operation *firstOperation = nullptr;
        bool safe = true;
        SmallVector<Value> pending{allocation.getResult()};
        llvm::SmallDenseSet<Value> visited;
        SmallVector<Operation *> aliasAccesses;
        while (!pending.empty() && safe) {
          Value value = pending.pop_back_val();
          if (!visited.insert(value).second) continue;
          for (OpOperand &use : value.getUses()) {
            Operation *owner = use.getOwner();
            Operation *root = rootInBlock(owner, &block);
            if (!root) { safe = false; break; }
            unsigned position = order.lookup(root);
            first = std::min(first, position);
            last = std::max(last, position);
            if (trackOwnedReshapes &&
                isa<memref::ExpandShapeOp, memref::CollapseShapeOp>(owner)) {
              // Whole-buffer identity-layout views only. Definitions count in
              // the lifetime too, so RAUW cannot create invalid dominance.
              auto source = dyn_cast<MemRefType>(value.getType());
              auto target = dyn_cast<MemRefType>(owner->getResult(0).getType());
              if (owner->getBlock() != &block || use.getOperandNumber() != 0 ||
                  !source || !target || !source.hasStaticShape() ||
                  !target.hasStaticShape() || !source.getLayout().isIdentity() ||
                  !target.getLayout().isIdentity() ||
                  source.getMemorySpace() != target.getMemorySpace() ||
                  source.getElementType() != target.getElementType() ||
                  !checkedElements(source) || !checkedElements(target) ||
                  checkedElements(source) != checkedElements(target)) {
                safe = false; break;
              }
              pending.push_back(owner->getResult(0));
              continue;
            }
            // Subviews/casts, frees, returns and opaque calls still reject.
            // Nested data accesses occupy their whole enclosing operation.
            if (!isa<memref::CopyOp, memref::LoadOp, memref::StoreOp,
                     linalg::GenericOp, linalg::FillOp>(owner)) {
              safe = false; break;
            }
            if (value != allocation.getResult()) aliasAccesses.push_back(root);
            if (position < firstData) {
              firstData = position; firstOperation = root;
            }
          }
        }
        // Initialization must directly cover the original buffer, with no
        // alias access in the same operation that could read its old contents.
        if (safe && firstOperation &&
            !llvm::is_contained(aliasAccesses, firstOperation) &&
            fullWrite(firstOperation, allocation.getResult(), &block))
          candidates.push_back({allocation, first, last});
      }
      return candidates;
}

struct BufferReuse : PassWrapper<BufferReuse, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(BufferReuse)
  StringRef getArgument() const final { return "llm2fpga-reuse-buffers"; }
  StringRef getDescription() const final {
    return "Share fully initialized private scratch with disjoint lifetimes";
  }
  void runOnOperation() override {
    getOperation().walk([&](func::FuncOp function) {
      if (function.isExternal() || !llvm::hasSingleElement(function.getBody()))
        return;
      Block &block = function.getBody().front();
      auto candidates = collectCandidates(block);
      llvm::stable_sort(candidates, [](const Candidate &a, const Candidate &b) {
        return a.first < b.first;
      });
      SmallVector<Candidate> slots;
      // All lifetimes are computed before rewriting. Equal enclosing-operation
      // positions overlap, including copy source/destination and branch uses.
      for (Candidate candidate : candidates) {
        auto slot = llvm::find_if(slots, [&](Candidate &slot) {
          return slot.last < candidate.first &&
                 slot.allocation.getType() == candidate.allocation.getType() &&
                 slot.allocation.getAlignment() == candidate.allocation.getAlignment();
        });
        if (slot == slots.end()) { slots.push_back(candidate); continue; }
        candidate.allocation.getResult().replaceAllUsesWith(
            slot->allocation.getResult());
        candidate.allocation.erase();
        slot->last = candidate.last;
      }
    });
  }
};
// Pool disjoint private buffers across static shapes without changing i64
// arithmetic. Views retain each original shape; the backing bank grows to the
// largest member. Every candidate still needs a proven full first write.
struct PrivateCapacityPool :
    PassWrapper<PrivateCapacityPool, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PrivateCapacityPool)
  PrivateCapacityPool() = default;
  PrivateCapacityPool(const PrivateCapacityPool &other) : PassWrapper(other) {}
  Option<bool> sharingStable{*this, "sharing-stable",
      llvm::cl::desc("Choose i1/i64 banks by lifetime order independent of capacity"),
      llvm::cl::init(false)};
  StringRef getArgument() const final { return "llm2fpga-pool-private-capacity"; }
  StringRef getDescription() const final {
    return "Pool fully initialized, disjoint private i64 buffers by capacity";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<memref::MemRefDialect>();
  }
  void runOnOperation() override {
    getOperation().walk([&](func::FuncOp function) {
      if (function.isExternal() || !llvm::hasSingleElement(function.getBody()))
        return;
      Block &block = function.getBody().front();
      auto candidates = collectCandidates(block, true);
      llvm::stable_sort(candidates, [](const Candidate &a, const Candidate &b) {
        return a.first < b.first;
      });
      struct Slot {
        SmallVector<Candidate> members;
        int64_t elements;
        unsigned last;
        uint64_t alignment;
        Type elementType;
      };
      SmallVector<Slot> slots;
      for (Candidate candidate : candidates) {
        auto type = candidate.allocation.getType();
        if ((!type.getElementType().isSignlessInteger(64) &&
             !(sharingStable && type.getElementType().isSignlessInteger(1))) ||
            !type.getLayout().isIdentity() || type.getMemorySpace()) continue;
        llvm::APInt size(64, 1);
        bool valid = true;
        for (int64_t dimension : type.getShape()) {
          bool overflow = false;
          size = size.smul_ov(llvm::APInt(64, dimension), overflow);
          if (overflow) { valid = false; break; }
        }
        if (!valid || size.isNegative() || size.isZero()) continue;
        int64_t elements = size.getSExtValue();
        uint64_t alignment = candidate.allocation.getAlignment().value_or(0);
        if (alignment > uint64_t(std::numeric_limits<int64_t>::max())) continue;
        unsigned best = slots.size();
        int64_t growth = std::numeric_limits<int64_t>::max();
        for (auto [index, slot] : llvm::enumerate(slots)) {
          if (slot.last >= candidate.first ||
              slot.elementType != type.getElementType()) continue;
          // Equal extents can make a growth-based allocator change alias
          // topology in one specialization. A stable first-fit policy chooses
          // from lifetime order alone; bounds still grow to the largest member.
          if (sharingStable) { best = index; break; }
          int64_t increase = std::max(elements, slot.elements) - slot.elements;
          if (best == slots.size() || increase < growth) {
            best = index; growth = increase;
          }
        }
        if (best == slots.size()) {
          slots.push_back({{candidate}, elements, candidate.last, alignment,
                           type.getElementType()});
        } else {
          auto &slot = slots[best];
          slot.members.push_back(candidate);
          slot.elements = std::max(slot.elements, elements);
          slot.last = candidate.last;
          slot.alignment = std::max(slot.alignment, alignment);
        }
      }
      OpBuilder builder(&getContext());
      for (Slot &slot : slots) {
        if (slot.members.size() < 2) continue;
        builder.setInsertionPointToStart(&block);
        auto alignment = slot.alignment
            ? builder.getI64IntegerAttr(slot.alignment) : IntegerAttr();
        auto shared = builder.create<memref::AllocOp>(
            slot.members.front().allocation.getLoc(),
            MemRefType::get({slot.elements}, slot.elementType),
            ValueRange{}, alignment);
        for (Candidate candidate : slot.members) {
          auto allocation = candidate.allocation;
          auto type = allocation.getType();
          builder.setInsertionPoint(allocation);
          // Bounded subviews and shape expansion are understood by the
          // existing fold-memref-alias-ops boundary; reinterpret casts are not.
          auto sliceType = type.getRank() == 0 ? type :
              MemRefType::get({type.getNumElements()}, type.getElementType());
          auto slice = builder.create<memref::SubViewOp>(
              allocation.getLoc(), sliceType, shared.getResult(),
              ArrayRef<int64_t>{0}, ArrayRef<int64_t>{type.getNumElements()},
              ArrayRef<int64_t>{1});
          Value replacement = slice.getResult();
          if (type.getRank() > 1) {
            SmallVector<ReassociationIndices> reassociation(1);
            for (int64_t i = 0; i < type.getRank(); ++i)
              reassociation.front().push_back(i);
            replacement = builder.create<memref::ExpandShapeOp>(
                allocation.getLoc(), type, replacement, reassociation).getResult();
          }
          allocation.getResult().replaceAllUsesWith(replacement);
          allocation.erase();
        }
      }
    });
  }
};
// Private functions called synchronously by one entrypoint can share scratch
// between calls, but never between simultaneous allocations within one helper.
// Exact types/alignment and a full first write are required. Public interfaces,
// aliasing buffers, nested/indirect calls and escaping allocations are excluded.
struct SequentialScratchPool :
    PassWrapper<SequentialScratchPool, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SequentialScratchPool)
  SequentialScratchPool() = default;
  SequentialScratchPool(const SequentialScratchPool &other) : PassWrapper(other) {}
  Option<std::string> entrypoint{*this, "entrypoint", llvm::cl::init("main")};
  StringRef getArgument() const final { return "llm2fpga-pool-sequential-scratch"; }
  StringRef getDescription() const final {
    return "Pool fully initialized private scratch across sequential calls";
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto root = module.lookupSymbol<func::FuncOp>(entrypoint);
    if (!root || root.isExternal() || !llvm::hasSingleElement(root.getBody())) return;
    Block &block = root.getBody().front();
    SmallVector<func::FuncOp> helpers;
    for (auto call : block.getOps<func::CallOp>()) {
      auto helper = module.lookupSymbol<func::FuncOp>(call.getCallee());
      if (!helper || helper == root || !helper.isPrivate() || helper.isExternal() ||
          !llvm::hasSingleElement(helper.getBody()) || llvm::is_contained(helpers, helper)) continue;
      auto uses = SymbolTable::getSymbolUses(helper, module);
      if (!uses || llvm::any_of(*uses, [&](const SymbolTable::SymbolUse &use) {
        auto caller = dyn_cast<func::CallOp>(use.getUser());
        return !caller || caller->getBlock() != &block;
      })) continue;
      helpers.push_back(helper);
    }
    using Key = std::pair<Type, Attribute>;
    // Count distinct helpers needing each slot before moving any allocation.
    // Repeated calls of one helper already reuse its private hardware storage.
    llvm::DenseMap<Key, SmallVector<unsigned>> users;
    for (auto helper : helpers) {
      llvm::DenseMap<Key, unsigned> nextSlot;
      for (auto candidate : collectCandidates(helper.getBody().front())) {
        auto allocation = candidate.allocation;
        Key key{allocation.getType(), allocation.getAlignmentAttr()};
        unsigned index = nextSlot[key]++;
        auto &counts = users[key];
        if (index == counts.size()) counts.push_back(0);
        ++counts[index];
      }
    }
    llvm::DenseMap<Key, SmallVector<Value>> pools;
    OpBuilder builder(&getContext());
    for (auto helper : helpers) {
      auto candidates = collectCandidates(helper.getBody().front());
      llvm::DenseMap<Key, unsigned> nextSlot;
      SmallVector<Value> arguments;
      for (auto candidate : candidates) {
        auto allocation = candidate.allocation;
        Key key{allocation.getType(), allocation.getAlignmentAttr()};
        unsigned index = nextSlot[key]++;
        if (users[key][index] < 2) continue;
        auto &slots = pools[key];
        if (index == slots.size()) {
          builder.setInsertionPointToStart(&block);
          auto shared = builder.create<memref::AllocOp>(allocation.getLoc(),
              allocation.getType(), ValueRange{}, allocation.getAlignmentAttr());
          slots.push_back(shared.getResult());
        }
        unsigned argument = helper.getNumArguments();
        helper.insertArgument(argument, allocation.getType(), DictionaryAttr{}, allocation.getLoc());
        allocation.getResult().replaceAllUsesWith(helper.getArgument(argument));
        allocation.erase();
        arguments.push_back(slots[index]);
      }
      if (!arguments.empty())
        for (auto call : block.getOps<func::CallOp>())
          if (call.getCallee() == helper.getSymName())
            call.getOperandsMutable().append(arguments);
    }
  }
};

bool pingPongPrivate(Value value, Block *block) {
  auto allocation = value.getDefiningOp<memref::AllocOp>();
  if (!allocation || allocation->getBlock() != block ||
      allocation.getNumOperands() != 0 ||
      !allocation->getDiscardableAttrDictionary().empty()) return false;
  auto type = allocation.getType();
  return type.hasStaticShape() && type.getRank() > 0 &&
         type.getElementType().isInteger(64) && type.getLayout().isIdentity() &&
         llvm::all_of(type.getShape(), [](int64_t size) { return size > 0; });
}

bool pingPongElementMap(AffineMap map, ArrayRef<int64_t> ranges) {
  if (map.getNumSymbols() || map.getNumDims() != ranges.size() ||
      map.getNumResults() != ranges.size()) return false;
  for (auto [dimension, expression] : llvm::enumerate(map.getResults())) {
    if (auto index = dyn_cast<AffineDimExpr>(expression)) {
      if (index.getPosition() != dimension) return false;
    } else if (auto constant = dyn_cast<AffineConstantExpr>(expression)) {
      if (constant.getValue() != 0 || ranges[dimension] != 1) return false;
    } else return false;
  }
  return true;
}

bool pingPongWholeMap(linalg::GenericOp operation, Value output) {
  if (!operation || !operation.hasPureBufferSemantics() ||
      operation.getOutputs().size() != 1 || operation.getOutputs()[0] != output ||
      !fullWrite(operation, output, operation->getBlock()) ||
      llvm::any_of(operation.getIteratorTypesArray(), [](utils::IteratorType type) {
        return type != utils::IteratorType::parallel;
      })) return false;
  for (Operation &nested : operation.getBody()->getOperations())
    if (nested.getNumRegions() || !isMemoryEffectFree(&nested)) return false;
  auto ranges = operation.getStaticLoopRanges();
  auto type = cast<MemRefType>(output.getType());
  if (ranges.size() != static_cast<size_t>(type.getRank()) ||
      !llvm::equal(ranges, type.getShape())) return false;
  auto &operand = operation->getOpOperand(operation.getInputs().size());
  return pingPongElementMap(operation.getMatchingIndexingMap(&operand), ranges);
}

bool pingPongNoEarlierExposure(Value source, linalg::GenericOp first,
                               linalg::GenericOp second) {
  Block *block = first->getBlock();
  for (OpOperand &use : source.getUses()) {
    Operation *owner = use.getOwner();
    if (owner == first || owner == second) continue;
    Operation *root = rootInBlock(owner, block);
    if (!root) return false;
    // The complete A value is identical after the second map. Later aliases
    // (e.g. a collapse_shape for output copy/argmax) therefore remain valid.
    if (second->isBeforeInBlock(root)) continue;
    if (owner != root || root == first || root == second ||
        first->isBeforeInBlock(root)) return false;
    if (isa<linalg::GenericOp, linalg::FillOp, memref::CopyOp>(owner)) continue;
    if (auto load = dyn_cast<memref::LoadOp>(owner)) {
      if (load.getMemRef() == source) continue;
    }
    if (auto store = dyn_cast<memref::StoreOp>(owner)) {
      if (store.getMemRef() == source) continue;
    }
    return false;
  }
  return true;
}

bool coalescePingPong(linalg::GenericOp first, linalg::GenericOp second) {
  if (!first || !second || first.getOutputs().size() != 1 ||
      second.getOutputs().size() != 1) return false;
  Value source = second.getOutputs()[0], intermediate = first.getOutputs()[0];
  Block *block = first->getBlock();
  if (source == intermediate || !pingPongPrivate(source, block) ||
      !pingPongPrivate(intermediate, block) || source.getType() != intermediate.getType())
    return false;
  auto a = source.getDefiningOp<memref::AllocOp>();
  auto b = intermediate.getDefiningOp<memref::AllocOp>();
  if (a.getAlignment() != b.getAlignment() ||
      !pingPongWholeMap(first, intermediate) || !pingPongWholeMap(second, source) ||
      !pingPongNoEarlierExposure(source, first, second)) return false;
  bool readsSource = false, readsIntermediate = false;
  auto firstRanges = first.getStaticLoopRanges();
  auto secondRanges = second.getStaticLoopRanges();
  for (unsigned i = 0; i < first.getInputs().size(); ++i) {
    auto &operand = first->getOpOperand(i);
    if (operand.get() != source) continue;
    readsSource = true;
    if (!pingPongElementMap(first.getMatchingIndexingMap(&operand), firstRanges))
      return false;
  }
  for (OpOperand &use : intermediate.getUses()) {
    if (use.getOwner() == first && use.getOperandNumber() == first.getInputs().size())
      continue;
    if (use.getOwner() != second || use.getOperandNumber() >= second.getInputs().size())
      return false;
    readsIntermediate = true;
    if (!pingPongElementMap(second.getMatchingIndexingMap(&use), secondRanges))
      return false;
  }
  if (!readsSource || !readsIntermediate) return false;
  intermediate.replaceAllUsesWith(source);
  b.erase();
  return true;
}

struct PointwisePingPong : PassWrapper<PointwisePingPong, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PointwisePingPong)
  StringRef getArgument() const final { return "llm2fpga-coalesce-pointwise-pingpong"; }
  StringRef getDescription() const final {
    return "Coalesce private adjacent same-element A-to-B-to-A parallel maps";
  }
  void runOnOperation() override {
    getOperation().walk([&](func::FuncOp function) {
      if (function.isExternal() || !llvm::hasSingleElement(function.getBody())) return;
      Block &block = function.getBody().front();
      for (Operation &operation : llvm::make_early_inc_range(block)) {
        auto first = dyn_cast<linalg::GenericOp>(&operation);
        if (!first) continue;
        auto second = dyn_cast_or_null<linalg::GenericOp>(first->getNextNode());
        if (second) coalescePingPong(first, second);
      }
    });
  }
};

}
void llm2fpga::registerBufferReuse() { PassRegistration<BufferReuse>(); }
void llm2fpga::registerPrivateCapacityPool() { PassRegistration<PrivateCapacityPool>(); }

void llm2fpga::registerSequentialScratchPool() { PassRegistration<SequentialScratchPool>(); }

void llm2fpga::registerPointwisePingPong() { PassRegistration<PointwisePingPong>(); }
