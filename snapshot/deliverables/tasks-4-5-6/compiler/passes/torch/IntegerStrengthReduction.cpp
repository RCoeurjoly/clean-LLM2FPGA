// Exact scalar signed division/remainder and index scaling by powers of two.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct IntegerStrengthReduction
    : PassWrapper<IntegerStrengthReduction, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(IntegerStrengthReduction)
  IntegerStrengthReduction() = default;
  IntegerStrengthReduction(const IntegerStrengthReduction &other) : PassWrapper(other) {}
  Option<bool> indexScales{*this, "index-scales",
      llvm::cl::desc("Restore index powers of two only after structural sharing"),
      llvm::cl::init(false)};
  StringRef getArgument() const final {
    return "llm2fpga-strength-reduce-integer-constants";
  }
  StringRef getDescription() const final {
    return "Reduce signed scalar division and unflagged index powers of two exactly";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect>();
  }
  void runOnOperation() override {
    SmallVector<Operation *> operations;
    getOperation().walk([&](Operation *op) {
      if (isa<arith::DivSIOp, arith::RemSIOp, arith::MulIOp>(op)) operations.push_back(op);
    });
    for (Operation *op : operations) {
      Type type = op->getResult(0).getType();
      auto integer = dyn_cast<IntegerType>(type);
      bool index = isa<IndexType>(type);
      llvm::APInt divisor;
      if (auto multiply = dyn_cast<arith::MulIOp>(op)) {
        if (!indexScales || !index ||
            multiply.getOverflowFlags() != arith::IntegerOverflowFlags::none ||
            !op->getDiscardableAttrDictionary().empty() ||
            !matchPattern(multiply.getRhs(), m_ConstantInt(&divisor)) ||
            divisor.isNegative() || !divisor.isPowerOf2() ||
            divisor.getActiveBits() > 31) continue;
        OpBuilder builder(op);
        Value shift = arith::ConstantOp::create(builder, op->getLoc(),
            builder.getIndexAttr(divisor.logBase2()));
        Value result = arith::ShLIOp::create(builder, op->getLoc(), multiply.getLhs(), shift);
        multiply.replaceAllUsesWith(result);
        multiply.erase();
        continue;
      }
      if ((!index && (!integer || !integer.isSignless())) ||
          !matchPattern(op->getOperand(1), m_ConstantInt(&divisor)) ||
          divisor.isNegative() || !divisor.isPowerOf2()) continue;
      // The production index contract is 32 bits in Calyx and 64 bits on the
      // CPU. Keep constants positive in both domains without changing the type.
      if (index && divisor.getActiveBits() > 31) continue;
      OpBuilder builder(op);
      auto loc = op->getLoc();
      Value numerator = op->getOperand(0);
      Value zero = builder.create<arith::ConstantOp>(
          loc, builder.getIntegerAttr(type, 0));
      Value mask = builder.create<arith::ConstantOp>(
          loc, builder.getIntegerAttr(type, divisor - 1));
      Value shift = builder.create<arith::ConstantOp>(
          loc, builder.getIntegerAttr(type, divisor.logBase2()));
      Value negative = builder.create<arith::CmpIOp>(
          loc, arith::CmpIPredicate::slt, numerator, zero);
      // Bias only negative inputs: this converts floor shift to truncation.
      // Negative input + (divisor-1) stays within the signed range, including
      // INT_MIN; nonnegative inputs get no bias. No overflow flags are added.
      Value bias = builder.create<arith::SelectOp>(loc, negative, mask, zero);
      Value adjusted = builder.create<arith::AddIOp>(loc, numerator, bias);
      Value quotient = builder.create<arith::ShRSIOp>(loc, adjusted, shift);
      Value result = quotient;
      if (isa<arith::RemSIOp>(op)) {
        Value product = builder.create<arith::ShLIOp>(loc, quotient, shift);
        result = builder.create<arith::SubIOp>(loc, numerator, product);
      }
      op->getResult(0).replaceAllUsesWith(result);
      op->erase();
    }
  }
};

// Flattening can encode otherwise identical strides as either shli or muli.
// Give structural function sharing one form, before canonicalization selects
// cheap constant operations again. This changes no arithmetic width or flags.
struct NormalizeIndexScales
    : PassWrapper<NormalizeIndexScales, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NormalizeIndexScales)
  StringRef getArgument() const final {
    return "llm2fpga-normalize-index-scales-for-sharing";
  }
  StringRef getDescription() const final {
    return "Normalize unflagged constant index shifts into modular multiplies";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect>();
  }
  void runOnOperation() override {
    SmallVector<arith::ShLIOp> shifts;
    getOperation().walk([&](arith::ShLIOp op) { shifts.push_back(op); });
    for (auto op : shifts) {
      APInt count;
      if (!isa<IndexType>(op.getType()) ||
          op.getOverflowFlags() != arith::IntegerOverflowFlags::none ||
          !op->getDiscardableAttrDictionary().empty() ||
          !matchPattern(op.getRhs(), m_ConstantInt(&count)) ||
          count.isNegative() || count.uge(31)) continue;
      // Index arithmetic is modular at the target width. For counts0..30,
      // x<<count == x*(1<<count) in both Calyx32 and CPU64 index domains;
      // the factor itself stays positive and representable in both.
      OpBuilder builder(op);
      Value factor = arith::ConstantOp::create(builder, op.getLoc(),
          builder.getIndexAttr(int64_t(1) << count.getZExtValue()));
      Value product = arith::MulIOp::create(builder, op.getLoc(), op.getLhs(), factor);
      op.replaceAllUsesWith(product);
      op.erase();
    }
  }
};
}
void llm2fpga::registerIntegerStrengthReduction() {
  PassRegistration<IntegerStrengthReduction>();
  PassRegistration<NormalizeIndexScales>();
}
