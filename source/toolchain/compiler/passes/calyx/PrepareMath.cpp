#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/Dialect/Math/Transforms/Passes.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"

using namespace mlir;
namespace {
struct Floor : OpRewritePattern<math::FloorOp> {
  using OpRewritePattern::OpRewritePattern;
  LogicalResult matchAndRewrite(math::FloorOp op, PatternRewriter &b) const final {
    auto negative = arith::NegFOp::create(b, op.getLoc(), op.getOperand());
    auto ceil = math::CeilOp::create(b, op.getLoc(), negative);
    auto floor = arith::NegFOp::create(b, op.getLoc(), ceil);
    b.replaceOpWithNewOp<math::CopySignOp>(op, floor, op.getOperand());
    return success();
  }
};
struct PrepareMath : PassWrapper<PrepareMath, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PrepareMath)
  StringRef getArgument() const final { return "prepare-calyx-math"; }
  StringRef getDescription() const final { return "Expand math with MLIR approximations"; }
  void getDependentDialects(DialectRegistry &r) const final {
    r.insert<arith::ArithDialect, math::MathDialect>();
  }
  void runOnOperation() final {
    RewritePatternSet patterns(&getContext());
    populateMathPolynomialApproximationPatterns(patterns, [](StringRef name) {
      return name == "math.exp" || name == "math.tanh";
    });
    math::populateExpansionPatterns(patterns, {"rsqrt", "fpowi", "ceil"});
    patterns.add<Floor>(&getContext());
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns))))
      signalPassFailure();
  }
};
}
void registerPrepareMathPass() { PassRegistration<PrepareMath>(); }
