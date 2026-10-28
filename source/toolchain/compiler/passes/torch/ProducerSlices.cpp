// Compute a sole consumer's static slice through the upstream tiling interface.
#include "TypedTensor.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/Linalg/Transforms/TilingInterfaceImpl.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/Dialect/Tensor/Transforms/Transforms.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "mlir/Interfaces/TilingInterface.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
using namespace mlir;
namespace {
struct FoldProducerSlice : OpRewritePattern<tensor::ExtractSliceOp> {
  using OpRewritePattern::OpRewritePattern;
  LogicalResult matchAndRewrite(tensor::ExtractSliceOp slice,
                                PatternRewriter &rewriter) const override {
    Operation *producer = slice.getSource().getDefiningOp();
    if (!producer || !isa<linalg::GenericOp, linalg::FillOp>(producer) ||
        producer->getNumResults() != 1 || !producer->hasOneUse())
      return failure();
    auto sourceType = dyn_cast<RankedTensorType>(slice.getSource().getType());
    auto resultType = dyn_cast<RankedTensorType>(slice.getType());
    if (!sourceType || !resultType || !sourceType.hasStaticShape() ||
        !resultType.hasStaticShape() || sourceType.getRank() != resultType.getRank() ||
        sourceType.getShape() == resultType.getShape() ||
        llvm::any_of(slice.getStaticStrides(), [](int64_t stride) { return stride != 1; }))
      return failure();
    // Tiling introduces initializer slices too. Full-size slices must not be
    // retiled indefinitely, and dynamic/out-of-domain bounds stay unchanged.
    for (int64_t dim = 0; dim < sourceType.getRank(); ++dim) {
      int64_t offset = slice.getStaticOffsets()[dim];
      int64_t size = slice.getStaticSizes()[dim];
      int64_t extent = sourceType.getDimSize(dim);
      if (offset < 0 || offset > extent || size <= 0 || size > extent - offset)
        return failure();
    }
    // The upstream result-tiling implementation requires a projected
    // permutation. Constant result indices (for example a kept dimension of
    // size one) are valid Linalg but unsupported by that implementation.
    auto linalgOp = cast<linalg::LinalgOp>(producer);
    if (!linalgOp.getIndexingMapsArray().back().isProjectedPermutation())
      return failure();
    // No observable effects may be skipped when removing unconsumed rows.
    bool pure = true;
    producer->walk([&](Operation *op) {
      if (op != producer && !isMemoryEffectFree(op)) pure = false;
    });
    if (!pure) return failure();
    // The upstream interface derives input tiles from indexing maps and keeps
    // untiled reduction dimensions. No model geometry or arithmetic is rebuilt.
    auto tiling = dyn_cast<TilingInterface>(producer);
    if (!tiling) return failure();
    auto tiled = tiling.generateResultTileValue(
        rewriter, 0, slice.getMixedOffsets(), slice.getMixedSizes());
    if (failed(tiled)) return failure();
    if (tiled->tiledValues.size() != 1 ||
        tiled->tiledValues.front().getType() != slice.getType()) {
      slice.emitError("producer_slice_contract: unexpected tile result type");
      return failure();
    }
    rewriter.replaceOp(slice, tiled->tiledValues);
    if (producer->use_empty()) rewriter.eraseOp(producer);
    return success();
  }
};
struct ProducerSlices : PassWrapper<ProducerSlices, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ProducerSlices)
  StringRef getArgument() const final { return "llm2fpga-fold-producer-slices"; }
  StringRef getDescription() const final {
    return "Tile pure single-use tensor producers for static unit-stride slices";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<linalg::LinalgDialect, tensor::TensorDialect, arith::ArithDialect>();
    linalg::registerTilingInterfaceExternalModels(registry);
  }
  void runOnOperation() override {
    RewritePatternSet patterns(&getContext());
    patterns.add<FoldProducerSlice>(&getContext());
    tensor::populateFoldTensorEmptyPatterns(patterns, /*foldSingleUseOnly=*/true);
    GreedyRewriteConfig config;
    config.setMaxNumRewrites(100000);
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns), config)))
      signalPassFailure();
  }
};
}
void llm2fpga::registerProducerSlices() { PassRegistration<ProducerSlices>(); }
