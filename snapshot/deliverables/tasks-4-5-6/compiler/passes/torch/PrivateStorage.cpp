// Functional tensor inputs are immutable at the ABI. Private storage dimensions
// may shrink only when every direct access uses the same valid constant index.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct FunctionalInputs : PassWrapper<FunctionalInputs, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(FunctionalInputs)
  StringRef getArgument() const final { return "llm2fpga-protect-functional-inputs"; }
  StringRef getDescription() const final { return "Protect immutable tensor inputs before native bufferization"; }
  void runOnOperation() override {
    SmallVector<std::pair<func::FuncOp, unsigned>> arguments;
    bool valid = true;
    getOperation().walk([&](func::FuncOp function) {
      for (auto [i, type] : llvm::enumerate(function.getArgumentTypes())) {
        if (!isa<TensorType>(type)) continue;
        auto attribute = function.getArgAttr(i, "bufferization.writable");
        if (attribute) {
          auto writable = dyn_cast<BoolAttr>(attribute);
          if (!writable || writable.getValue()) {
            function.emitError("functional input contract requires read-only tensor arguments");
            valid = false;
          }
        }
        arguments.emplace_back(function, i);
      }
    });
    if (!valid) { signalPassFailure(); return; }
    for (auto [function, index] : arguments)
      function.setArgAttr(index, "bufferization.writable", BoolAttr::get(&getContext(), false));
  }
};

struct FixedDimStorage : PassWrapper<FixedDimStorage, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(FixedDimStorage)
  StringRef getArgument() const final { return "llm2fpga-compact-fixed-dim-storage"; }
  StringRef getDescription() const final { return "Compact unobserved fixed dimensions of private indexed storage"; }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect, memref::MemRefDialect>();
  }
  void runOnOperation() override {
    getOperation().walk([&](memref::AllocOp allocation) {
      auto type = allocation.getType();
      if (!type.hasStaticShape() || !type.getLayout().isIdentity() ||
          allocation.getNumOperands() || allocation.getResult().use_empty() ||
          !allocation->getDiscardableAttrDictionary().empty() ||
          llvm::any_of(type.getShape(), [](int64_t size) { return size <= 0; })) return;
      SmallVector<Operation *> accesses;
      SmallVector<std::optional<int64_t>> fixed(type.getRank());
      SmallVector<bool> possible(type.getRank(), true);
      for (auto &use : allocation.getResult().getUses()) {
        Operation *op = use.getOwner();
        ValueRange indices;
        if (auto load = dyn_cast<memref::LoadOp>(op)) {
          if (load.getMemRef() != allocation.getResult()) return;
          indices = load.getIndices();
        } else if (auto store = dyn_cast<memref::StoreOp>(op)) {
          if (store.getMemRef() != allocation.getResult()) return;
          indices = store.getIndices();
        } else return; // No aliases, escapes, shape/stride observers or opaque uses.
        accesses.push_back(op);
        for (auto [dimension, index] : llvm::enumerate(indices)) {
          APInt value;
          if (!matchPattern(index, m_ConstantInt(&value))) {
            possible[dimension] = false; continue;
          }
          int64_t position = value.getSExtValue();
          if (position < 0 || position >= type.getDimSize(dimension)) return;
          if (fixed[dimension] && *fixed[dimension] != position)
            possible[dimension] = false;
          else fixed[dimension] = position;
        }
      }
      SmallVector<int64_t> shape(type.getShape());
      bool changed = false;
      for (unsigned i = 0; i < shape.size(); ++i)
        if (possible[i] && fixed[i] && shape[i] > 1) { shape[i] = 1; changed = true; }
      if (!changed) return;
      OpBuilder builder(allocation);
      Value zero = arith::ConstantIndexOp::create(builder, allocation.getLoc(), 0);
      // Each logical access maps bijectively onto the compact region. No other
      // use can observe the old extent, offset, layout or allocation identity.
      allocation.getResult().setType(MemRefType::get(shape, type.getElementType(),
          MemRefLayoutAttrInterface{}, type.getMemorySpace()));
      for (auto *op : accesses) {
        unsigned indexStart = op->getNumOperands() - shape.size();
        for (unsigned i = 0; i < shape.size(); ++i)
          if (shape[i] != type.getDimSize(i)) op->setOperand(indexStart + i, zero);
      }
    });
  }
};
}
void llm2fpga::registerPrivateStorage() {
  PassRegistration<FunctionalInputs>();
  PassRegistration<FixedDimStorage>();
}
