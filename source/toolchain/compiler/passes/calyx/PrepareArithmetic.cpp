#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/ControlFlow/IR/ControlFlowOps.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/DialectResourceBlobManager.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include <cmath>

using namespace mlir;

namespace {
struct PrepareArithmetic : PassWrapper<PrepareArithmetic, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PrepareArithmetic)
  StringRef getArgument() const final { return "prepare-calyx-arithmetic"; }
  StringRef getDescription() const final { return "Legalize arithmetic for Calyx"; }
  void getDependentDialects(DialectRegistry &registry) const final {
    registry.insert<arith::ArithDialect, memref::MemRefDialect, scf::SCFDialect>();
  }
  void runOnOperation() final {
    IRRewriter b(&getContext());
    getOperation().walk([&](Operation *op) {
      b.setInsertionPoint(op);
      auto loc = op->getLoc();
      if (auto global = dyn_cast<memref::GlobalOp>(op)) {
        if (auto resource = dyn_cast_or_null<DenseResourceElementsAttr>(
                global.getInitialValueAttr())) {
          global.setInitialValueAttr(DenseElementsAttr::getFromRawBuffer(
              resource.getType(), resource.getRawHandle().getBlob()->getData()));
        }
      } else if (auto copy = dyn_cast<memref::CopyOp>(op)) {
        auto type = llvm::cast<MemRefType>(copy.getSource().getType());
        if (!type.hasStaticShape()) {
          copy.emitError("Calyx memory copy requires a static shape");
          signalPassFailure();
          return;
        }
        auto zero = arith::ConstantIndexOp::create(b, loc, 0);
        auto one = arith::ConstantIndexOp::create(b, loc, 1);
        SmallVector<Value> indices;
        for (int64_t size : type.getShape()) {
          auto upper = arith::ConstantIndexOp::create(b, loc, size);
          auto loop = scf::ForOp::create(b, loc, zero, upper, one);
          indices.push_back(loop.getInductionVar());
          b.setInsertionPointToStart(loop.getBody());
        }
        auto value = memref::LoadOp::create(b, loc, copy.getSource(), indices);
        memref::StoreOp::create(b, loc, value, copy.getTarget(), indices);
        b.eraseOp(op);
      } else if (auto neg = dyn_cast<arith::NegFOp>(op)) {
        auto type = b.getIntegerType(neg.getType().getIntOrFloatBitWidth());
        auto sign = arith::ConstantOp::create(b, loc,
            b.getIntegerAttr(type, llvm::APInt::getSignMask(type.getWidth())));
        auto bits = arith::BitcastOp::create(b, loc, type, neg.getOperand());
        auto result = arith::XOrIOp::create(b, loc, bits, sign);
        b.replaceOpWithNewOp<arith::BitcastOp>(op, neg.getType(), result);
      } else if (auto round = dyn_cast<math::RoundEvenOp>(op)) {
        auto type = llvm::cast<FloatType>(round.getType());
        auto integer = b.getIntegerType(type.getWidth());
        auto signMask = llvm::APInt::getSignMask(type.getWidth());
        auto mask = arith::ConstantOp::create(b, loc, b.getIntegerAttr(integer, signMask));
        auto rest = arith::ConstantOp::create(b, loc, b.getIntegerAttr(integer, ~signMask));
        auto bits = arith::BitcastOp::create(b, loc, integer, round.getOperand());
        auto magnitudeBits = arith::AndIOp::create(b, loc, bits, rest);
        auto magnitude = arith::BitcastOp::create(b, loc, type, magnitudeBits);
        // At 2^(precision-1), one ULP is 1: addition rounds to an even integer.
        auto magic = arith::ConstantOp::create(b, loc,
            b.getFloatAttr(type, std::ldexp(1.0, type.getFPMantissaWidth() - 1)));
        auto sum = arith::AddFOp::create(b, loc, magnitude, magic);
        auto rounded = arith::SubFOp::create(b, loc, sum, magic);
        auto roundedBits = arith::BitcastOp::create(b, loc, integer, rounded);
        auto sign = arith::AndIOp::create(b, loc, bits, mask);
        auto signedBits = arith::OrIOp::create(b, loc, roundedBits, sign);
        auto value = arith::BitcastOp::create(b, loc, type, signedBits);
        auto small = arith::CmpFOp::create(b, loc, arith::CmpFPredicate::OLT, magnitude, magic);
        auto result = arith::SelectOp::create(b, loc, small, value, round.getOperand());
        b.replaceOp(op, result.getResult());
      } else if (auto cast = dyn_cast<arith::TruncFOp>(op)) {
        if (cast.getRoundingmode().value_or(arith::RoundingMode::to_nearest_even) !=
            arith::RoundingMode::to_nearest_even) {
          cast.emitError("unsupported floating-point rounding mode");
          signalPassFailure();
          return;
        }
        auto constant = cast.getIn().getDefiningOp<arith::ConstantFloatOp>();
        if (!constant) return;
        auto value = constant.value();
        auto type = llvm::cast<FloatType>(cast.getType());
        bool losesInfo;
        value.convert(type.getFloatSemantics(), llvm::APFloat::rmNearestTiesToEven, &losesInfo);
        auto result = arith::ConstantOp::create(b, loc, FloatAttr::get(type, value));
        b.replaceOp(op, result.getResult());
      } else if (isa<arith::MinimumFOp, arith::MaximumFOp>(op)) {
        bool minimum = isa<arith::MinimumFOp>(op);
        auto lhs = op->getOperand(0), rhs = op->getOperand(1);
        auto predicate = minimum ? arith::CmpFPredicate::ULT : arith::CmpFPredicate::UGT;
        auto compare = arith::CmpFOp::create(b, loc, predicate, lhs, rhs);
        auto selected = arith::SelectOp::create(b, loc, compare, lhs, rhs);
        auto equal = arith::CmpFOp::create(b, loc, arith::CmpFPredicate::OEQ, lhs, rhs);
        auto bits = arith::BitcastOp::create(b, loc,
            b.getIntegerType(lhs.getType().getIntOrFloatBitWidth()), lhs);
        auto zero = arith::ConstantOp::create(b, loc, b.getIntegerAttr(bits.getType(), 0));
        auto negative = arith::CmpIOp::create(b, loc, arith::CmpIPredicate::slt, bits, zero);
        auto tie = arith::SelectOp::create(b, loc, negative,
            minimum ? lhs : rhs, minimum ? rhs : lhs);
        auto result = arith::SelectOp::create(b, loc, equal, tie, selected);
        auto nan = arith::CmpFOp::create(b, loc, arith::CmpFPredicate::UNO, rhs, rhs);
        auto output = arith::SelectOp::create(b, loc, nan, rhs, result);
        b.replaceOp(op, output.getResult());
      } else if (auto cast = dyn_cast<arith::UIToFPOp>(op)) {
        if (!cast.getIn().getType().isInteger(1)) return;
        auto type = cast.getType();
        auto zero = arith::ConstantOp::create(b, loc, b.getFloatAttr(type, 0.0));
        auto one = arith::ConstantOp::create(b, loc, b.getFloatAttr(type, 1.0));
        auto result = arith::SelectOp::create(b, loc, cast.getIn(), one, zero);
        b.replaceOp(op, result.getResult());
      } else if (isa<math::CopySignOp, math::AbsFOp>(op)) {
        auto resultType = op->getResult(0).getType();
        auto type = b.getIntegerType(resultType.getIntOrFloatBitWidth());
        auto mask = llvm::APInt::getSignMask(type.getWidth());
        auto rest = arith::ConstantOp::create(b, loc, b.getIntegerAttr(type, ~mask));
        auto magnitude = arith::BitcastOp::create(b, loc, type, op->getOperand(0));
        auto low = arith::AndIOp::create(b, loc, magnitude, rest);
        Value bits = low;
        if (isa<math::CopySignOp>(op)) {
          auto sign = arith::ConstantOp::create(b, loc, b.getIntegerAttr(type, mask));
          auto source = arith::BitcastOp::create(b, loc, type, op->getOperand(1));
          auto high = arith::AndIOp::create(b, loc, source, sign);
          bits = arith::OrIOp::create(b, loc, high, low);
        }
        auto result = arith::BitcastOp::create(b, loc, resultType, bits);
        b.replaceOp(op, result.getResult());
      } else if (auto assertion = dyn_cast<cf::AssertOp>(op)) {
        // An assertion failure halts the accelerator: the invocation cannot finish.
        auto one = arith::ConstantIntOp::create(b, loc, 1, 1);
        auto failed = arith::XOrIOp::create(b, loc, assertion.getArg(), one);
        auto loop = scf::WhileOp::create(b, loc, TypeRange{}, ValueRange{});
        auto &before = loop.getBefore().emplaceBlock();
        b.setInsertionPointToStart(&before);
        scf::ConditionOp::create(b, loc, failed, ValueRange{});
        auto &after = loop.getAfter().emplaceBlock();
        b.setInsertionPointToStart(&after);
        scf::YieldOp::create(b, loc);
        b.eraseOp(op);
      }
    });
  }
};
}

void registerPrepareArithmeticPass() { PassRegistration<PrepareArithmetic>(); }
