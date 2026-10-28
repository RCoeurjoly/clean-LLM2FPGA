// Verification-only entry adaptation for the MLIR runner's scalar ABI.
// This pass is never part of the production core profile.
#include "TypedTensor.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct TestScalarEntry : PassWrapper<TestScalarEntry, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(TestScalarEntry)
  StringRef getArgument() const final { return "llm2fpga-test-scalar-entry"; }
  StringRef getDescription() const final { return "Extract a computed scalar tensor for verification ABI"; }
  void getDependentDialects(DialectRegistry &registry) const final {
    registry.insert<tensor::TensorDialect>();
  }
  void runOnOperation() final {
    auto entry = getOperation().lookupSymbol<func::FuncOp>("main");
    if (!entry || entry.isDeclaration() || entry.getFunctionType().getNumInputs() != 0 ||
        entry.getFunctionType().getNumResults() != 1) {
      getOperation().emitError("test scalar entry requires a defined no-argument main with one result");
      signalPassFailure(); return;
    }
    auto tensor = dyn_cast<RankedTensorType>(entry.getFunctionType().getResult(0));
    if (!tensor || tensor.getRank() != 0 || !tensor.getElementType().isSignlessInteger(64)) {
      entry.emitError("test scalar entry requires a computed rank-zero i64 tensor");
      signalPassFailure(); return;
    }
    entry.walk([&](func::ReturnOp ret) {
      OpBuilder builder(ret);
      auto scalar = tensor::ExtractOp::create(builder, ret.getLoc(), ret.getOperand(0), ValueRange{});
      ret.setOperand(0, scalar);
    });
    entry.setType(FunctionType::get(entry.getContext(), {}, {tensor.getElementType()}));
  }
};
}
void llm2fpga::registerTestScalarEntry() { PassRegistration<TestScalarEntry>(); }
