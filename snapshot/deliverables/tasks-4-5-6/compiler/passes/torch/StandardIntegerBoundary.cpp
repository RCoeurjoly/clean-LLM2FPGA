#include "TypedTensor.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/StringSwitch.h"
using namespace mlir;
namespace {
static bool integerType(Type type) {
  if (isa<IntegerType, IndexType>(type)) return true;
  if (auto shaped = dyn_cast<ShapedType>(type)) return integerType(shaped.getElementType());
  return false;
}
struct StandardIntegerBoundary : PassWrapper<StandardIntegerBoundary, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(StandardIntegerBoundary)
  StringRef getArgument() const final { return "llm2fpga-check-standard-integer-ir"; }
  StringRef getDescription() const final { return "Reject remaining custom, Torch and floating-point computation"; }
  void runOnOperation() final {
    auto result = getOperation().walk([&](Operation *op) {
      StringRef dialect = op->getName().getDialectNamespace();
      bool standard = llvm::StringSwitch<bool>(dialect)
        .Cases({"builtin", "func", "arith", "tensor"}, true)
        .Cases({"linalg", "math", "scf", "cf"}, true)
        .Cases({"affine", "memref", "bufferization"}, true).Default(false);
      auto reject = [&]() {
        op->emitError("standard_integer_contract: non-standard dialect or non-integer type remains");
        return WalkResult::interrupt();
      };
      if (!standard) return reject();
      for (Type type : op->getResultTypes()) if (!integerType(type)) return reject();
      for (Region &region : op->getRegions())
        for (Block &block : region)
          for (BlockArgument argument : block.getArguments())
            if (!integerType(argument.getType())) return reject();
      if (auto function = dyn_cast<func::FuncOp>(op)) {
        for (Type type : function.getFunctionType().getInputs()) if (!integerType(type)) return reject();
        for (Type type : function.getFunctionType().getResults()) if (!integerType(type)) return reject();
      }
      return WalkResult::advance();
    });
    if (result.wasInterrupted()) signalPassFailure();
  }
};
}
void llm2fpga::registerStandardIntegerBoundary() { PassRegistration<StandardIntegerBoundary>(); }
