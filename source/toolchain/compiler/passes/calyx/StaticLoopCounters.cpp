// Specialize induction state from constant loop bounds, never sampled values.
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/MathExtras.h"
#include <limits>
#include <optional>

using namespace mlir;

namespace {
static std::optional<int64_t> constant(Value value) {
  auto op = value.getDefiningOp<arith::ConstantOp>();
  auto attribute = op ? dyn_cast<IntegerAttr>(op.getValue()) : IntegerAttr{};
  if (!attribute || attribute.getValue().getBitWidth() > 64) return {};
  return attribute.getValue().getSExtValue();
}

static Value widen(OpBuilder &builder, Location location, Value value, Type type) {
  if (type.isIndex()) return arith::IndexCastOp::create(builder, location, type, value);
  return arith::ExtSIOp::create(builder, location, type, value);
}

struct StaticLoopCounters : PassWrapper<StaticLoopCounters, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(StaticLoopCounters)
  StringRef getArgument() const final { return "llm2fpga-narrow-static-loop-counters"; }
  StringRef getDescription() const final {
    return "Narrow canonical positive induction state with proved constant bounds";
  }
  void runOnOperation() override {
    SmallVector<scf::WhileOp> loops;
    getOperation().walk([&](scf::WhileOp loop) { loops.push_back(loop); });
    for (auto loop : loops) {
      if (!loop.getNumOperands() || !loop.getBefore().hasOneBlock() ||
          !loop.getAfter().hasOneBlock()) continue;
      Block &before = loop.getBefore().front(), &after = loop.getAfter().front();
      auto condition = dyn_cast<scf::ConditionOp>(before.getTerminator());
      auto yield = dyn_cast<scf::YieldOp>(after.getTerminator());
      if (!condition || !yield || condition.getArgs().empty() || yield.getOperands().empty() ||
          condition.getArgs()[0] != before.getArgument(0)) continue;
      Type original = before.getArgument(0).getType();
      unsigned originalBits;
      if (original.isIndex()) {
        // The pinned SCF-to-Calyx route normalizes index values to signed i32.
        originalBits = 32;
      } else if (auto integer = dyn_cast<IntegerType>(original)) {
        if (!integer.isSignless() || integer.getWidth() > 64) continue;
        originalBits = integer.getWidth();
      } else continue;
      if (originalBits < 2) continue;
      auto compare = condition.getCondition().getDefiningOp<arith::CmpIOp>();
      if (!compare || compare.getLhs() != before.getArgument(0) ||
          (compare.getPredicate() != arith::CmpIPredicate::slt &&
           compare.getPredicate() != arith::CmpIPredicate::ult)) continue;
      auto increment = yield.getOperands()[0].getDefiningOp<arith::AddIOp>();
      if (!increment) continue;
      Value stepValue;
      if (increment.getLhs() == after.getArgument(0)) stepValue = increment.getRhs();
      else if (increment.getRhs() == after.getArgument(0)) stepValue = increment.getLhs();
      else continue;
      auto lower = constant(loop.getOperand(0)), upper = constant(compare.getRhs()),
           step = constant(stepValue);
      if (!lower || !upper || !step || *lower < 0 || *upper <= *lower || *step <= 0)
        continue;
      uint64_t maximum = originalBits == 64 ? std::numeric_limits<int64_t>::max()
                                           : (uint64_t{1} << (originalBits - 1)) - 1;
      if (static_cast<uint64_t>(*upper) > maximum ||
          static_cast<uint64_t>(*step - 1) > maximum - static_cast<uint64_t>(*upper))
        continue;
      // Includes the final update that exits the loop, not just body values.
      uint64_t last = static_cast<uint64_t>(*upper) + static_cast<uint64_t>(*step - 1);
      unsigned bits = llvm::Log2_64(last) + 2; // retain a zero sign bit
      if (bits >= originalBits) continue;
      OpBuilder builder(loop);
      auto narrow = builder.getIntegerType(bits);
      Value initial = arith::ConstantOp::create(builder, loop.getLoc(),
          builder.getIntegerAttr(narrow, *lower));
      loop.setOperand(0, initial);
      before.getArgument(0).setType(narrow);
      after.getArgument(0).setType(narrow);
      loop.getResult(0).setType(narrow);
      builder.setInsertionPointToStart(&before);
      Value beforeWide = widen(builder, loop.getLoc(), before.getArgument(0), original);
      before.getArgument(0).replaceAllUsesExcept(beforeWide, beforeWide.getDefiningOp());
      builder.setInsertionPoint(compare);
      Value limit = arith::ConstantOp::create(builder, compare.getLoc(),
          builder.getIntegerAttr(narrow, *upper));
      Value predicate = arith::CmpIOp::create(builder, compare.getLoc(),
          compare.getPredicate(), before.getArgument(0), limit);
      compare.getResult().replaceAllUsesWith(predicate);
      compare.erase();
      condition.setOperand(1, before.getArgument(0));
      builder.setInsertionPointToStart(&after);
      Value afterWide = widen(builder, loop.getLoc(), after.getArgument(0), original);
      after.getArgument(0).replaceAllUsesExcept(afterWide, afterWide.getDefiningOp());
      builder.setInsertionPoint(increment);
      Value stride = arith::ConstantOp::create(builder, increment.getLoc(),
          builder.getIntegerAttr(narrow, *step));
      Value next = arith::AddIOp::create(builder, increment.getLoc(), after.getArgument(0), stride);
      Value nextWide = widen(builder, increment.getLoc(), next, original);
      increment.getResult().replaceAllUsesWith(nextWide);
      increment.erase();
      yield.setOperand(0, next);
      builder.setInsertionPointAfter(loop);
      Value resultWide = widen(builder, loop.getLoc(), loop.getResult(0), original);
      loop.getResult(0).replaceAllUsesExcept(resultWide, resultWide.getDefiningOp());
    }
  }
};
}

void registerStaticLoopCountersPass() { PassRegistration<StaticLoopCounters>(); }
