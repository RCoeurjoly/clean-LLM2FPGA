// Preserve PyTorch's wrapping integer absolute value in scalar loop IR.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct IntegerMath : PassWrapper<IntegerMath, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(IntegerMath)
  StringRef getArgument() const final { return "llm2fpga-lower-integer-math"; }
  StringRef getDescription() const final { return "Lower scalar integer abs with wrapping subtraction"; }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect>();
  }
  void runOnOperation() override {
    SmallVector<math::AbsIOp> operations;
    bool invalid = false;
    getOperation().walk([&](math::AbsIOp op) {
      auto type = dyn_cast<IntegerType>(op.getType());
      if (!type || !type.isSignless()) {
        op.emitError("integer_math_contract: scalar signless integer required");
        invalid = true;
      } else operations.push_back(op);
    });
    if (invalid) { signalPassFailure(); return; }
    for (auto op : operations) {
      OpBuilder builder(op);
      auto type = cast<IntegerType>(op.getType());
      auto zero = builder.create<arith::ConstantIntOp>(op.getLoc(), 0, type.getWidth());
      auto negative = builder.create<arith::CmpIOp>(op.getLoc(), arith::CmpIPredicate::slt,
                                                    op.getOperand(), zero);
      // No overflow flags: abs(INT_MIN) remains INT_MIN, as in PyTorch.
      auto negated = builder.create<arith::SubIOp>(op.getLoc(), zero, op.getOperand());
      auto result = builder.create<arith::SelectOp>(op.getLoc(), negative, negated, op.getOperand());
      op.replaceAllUsesWith(result.getResult());
      op.erase();
    }
  }
};
}
void llm2fpga::registerIntegerMath() { PassRegistration<IntegerMath>(); }
