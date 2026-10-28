// Join independent pointwise branches without cloning their shared producers.
#include "TypedTensor.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
using namespace mlir;
namespace {
bool eligible(linalg::GenericOp op) {
  if (!op.hasPureTensorSemantics() || !op.isAllParallelLoops()) return false;
  auto ranges = op.getStaticLoopRanges();
  if (llvm::any_of(ranges, [](int64_t n) { return n <= 0; })) return false;
  for (OpOperand &output : op.getDpsInitsMutable()) {
    if (!op.getMatchingIndexingMap(&output).isIdentity()) return false;
    auto type = dyn_cast<RankedTensorType>(output.get().getType());
    if (!type || !type.hasStaticShape()) return false;
    if (!op.getRegion().front().getArgument(output.getOperandNumber()).use_empty())
      return false;
  }
  bool pure = true;
  op.getRegion().walk([&](Operation *nested) {
    if (!isMemoryEffectFree(nested)) pure = false;
  });
  return pure;
}
Operation *rootInBlock(Operation *op, Block *block) {
  while (op && op->getBlock() != block) op = op->getParentOp();
  return op;
}
bool canJoin(linalg::GenericOp first, linalg::GenericOp second) {
  if (!eligible(second) || first.getStaticLoopRanges() != second.getStaticLoopRanges())
    return false;
  bool common = false;
  for (Value input : first.getInputs())
    for (Value other : second.getInputs()) common |= input == other;
  if (!common) return false;
  // Move the first pure computation to the second. No dependent operation may
  // consume its result before (or inside) that insertion point.
  for (Value result : first.getResults())
    for (Operation *user : result.getUsers()) {
      auto root = rootInBlock(user, first->getBlock());
      if (!root || root == second || root->isBeforeInBlock(second)) return false;
    }
  return true;
}
struct JoinBranches : OpRewritePattern<linalg::GenericOp> {
  using OpRewritePattern::OpRewritePattern;
  LogicalResult matchAndRewrite(linalg::GenericOp first,
                                PatternRewriter &rewriter) const override {
    if (!eligible(first)) return failure();
    linalg::GenericOp second;
    unsigned examined = 0;
    // Bound search cost; failure to find a nearby sibling leaves valid IR intact.
    for (Operation *next = first->getNextNode(); next && examined++ < 64;
         next = next->getNextNode()) {
      auto candidate = dyn_cast<linalg::GenericOp>(next);
      if (candidate && canJoin(first, candidate)) { second = candidate; break; }
    }
    if (!second) return failure();
    SmallVector<Value> inputs, outputs;
    SmallVector<AffineMap> inputMaps, outputMaps;
    SmallVector<SmallVector<unsigned>> positions;
    SmallVector<Type> results;
    for (auto op : {first, second}) {
      SmallVector<unsigned> mapping;
      for (OpOperand &input : op.getInputsMutable()) {
        auto map = op.getMatchingIndexingMap(&input);
        unsigned index = 0;
        while (index < inputs.size() &&
               (inputs[index] != input.get() || inputMaps[index] != map)) ++index;
        if (index == inputs.size()) { inputs.push_back(input.get()); inputMaps.push_back(map); }
        mapping.push_back(index);
      }
      positions.push_back(std::move(mapping));
      for (OpOperand &output : op.getDpsInitsMutable()) {
        outputs.push_back(output.get());
        outputMaps.push_back(op.getMatchingIndexingMap(&output));
      }
      llvm::append_range(results, op.getResultTypes());
    }
    SmallVector<AffineMap> maps(inputMaps);
    llvm::append_range(maps, outputMaps);
    SmallVector<utils::IteratorType> iterators(first.getNumLoops(), utils::IteratorType::parallel);
    rewriter.setInsertionPoint(second);
    auto joined = linalg::GenericOp::create(rewriter,
      FusedLoc::get(first.getContext(), {first.getLoc(), second.getLoc()}),
      results, inputs, outputs, maps, iterators,
      [&](OpBuilder &builder, Location loc, ValueRange arguments) {
        SmallVector<Value> yielded;
        unsigned outputOffset = inputs.size(), opIndex = 0;
        for (auto op : {first, second}) {
          IRMapping mapping;
          auto &body = op.getRegion().front();
          for (auto [index, position] : llvm::enumerate(positions[opIndex++]))
            mapping.map(body.getArgument(index), arguments[position]);
          for (unsigned i = 0; i < op.getNumDpsInits(); ++i)
            mapping.map(body.getArgument(op.getNumDpsInputs() + i), arguments[outputOffset++]);
          for (Operation &nested : body.without_terminator()) builder.clone(nested, mapping);
          for (Value result : body.getTerminator()->getOperands())
            yielded.push_back(mapping.lookupOrDefault(result));
        }
        linalg::YieldOp::create(builder, loc, yielded);
      });
    unsigned firstResults = first.getNumResults();
    rewriter.replaceOp(first, joined.getResults().take_front(firstResults));
    rewriter.replaceOp(second, joined.getResults().drop_front(firstResults));
    return success();
  }
};
struct HorizontalFusion : PassWrapper<HorizontalFusion, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(HorizontalFusion)
  StringRef getArgument() const final { return "llm2fpga-fuse-horizontal-pointwise"; }
  StringRef getDescription() const final { return "Join pure, independent pointwise branches in one domain"; }
  void runOnOperation() override {
    RewritePatternSet patterns(&getContext()); patterns.add<JoinBranches>(&getContext());
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns)))) signalPassFailure();
  }
};
}
void llm2fpga::registerHorizontalPointwiseFusion() { PassRegistration<HorizontalFusion>(); }
