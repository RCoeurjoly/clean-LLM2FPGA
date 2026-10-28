// Recover the exact scalar tails retained in the RC and full-model graphs.
// Their prefixes can straddle buffers or have folded high-bit steps. Treat the
// incoming remainder/result as arbitrary i64; make no range/positivity assumption.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseSet.h"
using namespace mlir;
namespace {
bool constant(Value value, uint64_t expected) {
  APInt number;
  return value.getType().isInteger(64) &&
         matchPattern(value, m_ConstantInt(&number)) &&
         number.getZExtValue() == expected;
}
bool wrapping(Operation *op) {
  if (auto add = dyn_cast<arith::AddIOp>(op))
    return add.getOverflowFlags() == arith::IntegerOverflowFlags::none;
  if (auto sub = dyn_cast<arith::SubIOp>(op))
    return sub.getOverflowFlags() == arith::IntegerOverflowFlags::none;
  return true;
}
bool addition(Value value, Value input, uint64_t bit,
              llvm::DenseSet<Operation *> &owned) {
  auto add = value.getDefiningOp<arith::AddIOp>();
  if (!add || !wrapping(add)) return false;
  if (!((add.getLhs() == input && constant(add.getRhs(), bit)) ||
        (add.getRhs() == input && constant(add.getLhs(), bit)))) return false;
  owned.insert(add);
  return true;
}
struct Tail {
  Value remainder, result;
  llvm::DenseSet<Operation *> owned;
};
bool matchTail(arith::SelectOp root, Tail &tail, unsigned steps) {
  if (!root.getType().isInteger(64)) return false;
  Value current = root.getResult(), remainderOutput;
  for (unsigned stage = 0; stage < steps; ++stage) {
    uint64_t bit = uint64_t(1) << (2*stage);
    auto select = current.getDefiningOp<arith::SelectOp>();
    if (!select) return false;
    auto compare = select.getCondition().getDefiningOp<arith::CmpIOp>();
    auto half = select.getFalseValue().getDefiningOp<arith::ShRSIOp>();
    if (!compare || compare.getPredicate() != arith::CmpIPredicate::sge ||
        !half || !constant(half.getRhs(), 1)) return false;
    Value oldResult = half.getLhs(), oldRemainder = compare.getLhs();
    if (!addition(select.getTrueValue(), half.getResult(), bit, tail.owned) ||
        !addition(compare.getRhs(), oldResult, bit, tail.owned)) return false;
    tail.owned.insert(select);
    tail.owned.insert(compare);
    tail.owned.insert(half);
    // Every predecessor remainder must use exactly the same compare/candidate
    // as its result. The final unused remainder is intentionally not required.
    if (remainderOutput) {
      auto rem = remainderOutput.getDefiningOp<arith::SelectOp>();
      if (!rem || rem.getCondition() != compare.getResult() ||
          rem.getFalseValue() != oldRemainder) return false;
      auto sub = rem.getTrueValue().getDefiningOp<arith::SubIOp>();
      if (!sub || !wrapping(sub) || sub.getLhs() != oldRemainder ||
          sub.getRhs() != compare.getRhs()) return false;
      tail.owned.insert(rem);
      tail.owned.insert(sub);
    }
    current = oldResult;
    remainderOutput = oldRemainder;
  }
  tail.result = current;
  tail.remainder = remainderOutput;
  // Reject escaped intermediates, cross-block chains.
  // Constants and the two arbitrary starting values are outside the closure.
  for (Operation *op : tail.owned) {
    if (op->getBlock() != root->getBlock()) return false;
    for (Value value : op->getResults()) {
      if (value == root.getResult()) continue;
      for (Operation *user : value.getUsers())
        if (!tail.owned.contains(user)) return false;
    }
  }
  return !tail.owned.contains(tail.result.getDefiningOp()) &&
         !tail.owned.contains(tail.remainder.getDefiningOp());
}
struct RestoringSqrtLoops
    : PassWrapper<RestoringSqrtLoops, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RestoringSqrtLoops)
  StringRef getArgument() const final {
    return "llm2fpga-recover-restoring-sqrt-loops";
  }
  StringRef getDescription() const final {
    return "Recover closed exact signed64 restoring-root tails as sequential steps";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect, scf::SCFDialect>();
  }
  void runOnOperation() override {
    SmallVector<arith::SelectOp> candidates;
    getOperation().walk([&](arith::SelectOp op) { candidates.push_back(op); });
    for (auto root : candidates) {
      Tail tail;
      unsigned steps = 31;
      if (!matchTail(root, tail, steps)) {
        // The full-model final norm retains 29 steps starting at 2^56; its
        // first step is folded, leaving a regular closed 28-step scalar tail.
        tail = Tail();
        steps = 28;
        if (!matchTail(root, tail, steps)) continue;
      }
      OpBuilder builder(root);
      Location loc = root.getLoc();
      auto lower = builder.create<arith::ConstantIndexOp>(loc, 0);
      auto upper = builder.create<arith::ConstantIndexOp>(loc, steps);
      auto step = builder.create<arith::ConstantIndexOp>(loc, 1);
      auto initialBit = builder.create<arith::ConstantIntOp>(loc, int64_t(1) << (2*(steps-1)), 64);
      auto one = builder.create<arith::ConstantIntOp>(loc, 1, 64);
      auto two = builder.create<arith::ConstantIntOp>(loc, 2, 64);
      auto loop = builder.create<scf::ForOp>(loc, lower, upper, step,
          ValueRange{tail.remainder, tail.result, initialBit},
          [&](OpBuilder &body, Location at, Value, ValueRange args) {
            auto candidate = body.create<arith::AddIOp>(at, args[1], args[2]);
            auto take = body.create<arith::CmpIOp>(at, arith::CmpIPredicate::sge,
                                                 args[0], candidate);
            auto sub = body.create<arith::SubIOp>(at, args[0], candidate);
            auto half = body.create<arith::ShRSIOp>(at, args[1], one);
            auto plus = body.create<arith::AddIOp>(at, half, args[2]);
            auto remainder = body.create<arith::SelectOp>(at, take, sub, args[0]);
            auto result = body.create<arith::SelectOp>(at, take, plus, half);
            auto nextBit = body.create<arith::ShRUIOp>(at, args[2], two);
            body.create<scf::YieldOp>(at, ValueRange{remainder, result, nextBit});
          });
      loop->setAttr("llm2fpga.restoring_sqrt_tail", builder.getUnitAttr());
      root.replaceAllUsesWith(loop.getResult(1));
      // Leave the now-dead, side-effect-free chain to the typed canonicalizer.
      // No other candidate handles are invalidated during this traversal.
    }
  }
};
}
void llm2fpga::registerRestoringSqrtLoops() {
  PassRegistration<RestoringSqrtLoops>();
}
