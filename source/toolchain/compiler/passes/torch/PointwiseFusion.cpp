// Reuse upstream fusion while excluding repeated producer work.
#include "TypedTensor.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/Linalg/Transforms/Transforms.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
using namespace mlir;
namespace {
struct PointwiseFusion : PassWrapper<PointwiseFusion, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PointwiseFusion)
  StringRef getArgument() const final { return "llm2fpga-fuse-pointwise"; }
  StringRef getDescription() const final {
    return "Fuse tensor pointwise producers without broadcast recomputation";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<linalg::LinalgDialect, tensor::TensorDialect>();
  }
  void runOnOperation() override {
    RewritePatternSet patterns(&getContext());
    linalg::populateElementwiseOpsFusionPatterns(patterns, [](OpOperand *operand) {
      auto consumer = dyn_cast<linalg::LinalgOp>(operand->getOwner());
      // Every producer element must be used once per consumer iteration domain.
      // A permutation input map visits each producer element exactly once,
      // including reductions. Projected broadcast maps would repeat work.
      return consumer && operand->get().hasOneUse() &&
             consumer.getMatchingIndexingMap(operand).isPermutation();
    });
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns))))
      signalPassFailure();
  }
};
}
void llm2fpga::registerPointwiseFusion() { PassRegistration<PointwiseFusion>(); }
