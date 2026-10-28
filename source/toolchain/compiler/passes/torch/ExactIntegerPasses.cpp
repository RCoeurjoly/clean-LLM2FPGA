#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
static LogicalResult lowerNegation(Operation *op) {
  if (op->getNumOperands() != 1 || op->getNumResults() != 1)
    return op->emitError("exact_integer_contract: negation arity differs");
  auto input = llm2fpga::staticI64Tensor(op->getOperand(0).getType());
  auto result = llm2fpga::staticI64Tensor(op->getResult(0).getType());
  if (!input || !result || *input != *result)
    return op->emitError("exact_integer_contract: negation requires equal static signed i64 tensors");
  OpBuilder builder(op);
  auto loc = op->getLoc();
  Value builtin = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(0), *input);
  Value empty = tensor::EmptyOp::create(builder, loc, input->getShape(), builder.getI64Type());
  auto map = AffineMap::getMultiDimIdentityMap(input->getRank(), builder.getContext());
  SmallVector<AffineMap> maps{map, map};
  SmallVector<utils::IteratorType> iterators(input->getRank(), utils::IteratorType::parallel);
  auto lowered = linalg::GenericOp::create(builder, loc, TypeRange{*result},
    ValueRange{builtin}, ValueRange{empty}, maps, iterators,
    [](OpBuilder &nested, Location bodyLoc, ValueRange args) {
      Value zero = arith::ConstantIntOp::create(nested, bodyLoc, 0, 64);
      Value negated = arith::SubIOp::create(nested, bodyLoc, zero, args[0]);
      linalg::YieldOp::create(nested, bodyLoc, negated);
    });
  Value converted = llm2fpga::bridge(builder, loc, "torch_c.from_builtin_tensor",
                                     lowered.getResult(0), op->getResult(0).getType());
  op->getResult(0).replaceAllUsesWith(converted);
  op->erase();
  return success();
}

// Tensor division must remain integer arithmetic. The pinned upstream route
// fails executable signed-extreme fixtures; do not delegate it to float division.
static LogicalResult lowerDivision(Operation *op) {
  if (op->getNumOperands() != 3 || op->getNumResults() != 1)
    return op->emitError("exact_integer_contract: division arity differs");
  auto lhs = llm2fpga::staticI64Tensor(op->getOperand(0).getType());
  auto rhs = llm2fpga::staticI64Tensor(op->getOperand(1).getType());
  auto result = llm2fpga::staticI64Tensor(op->getResult(0).getType());
  auto *modeOp = op->getOperand(2).getDefiningOp();
  auto mode = modeOp ? modeOp->getAttrOfType<StringAttr>("value") : StringAttr{};
  if (!lhs || !rhs || !result || !modeOp ||
      modeOp->getName().getStringRef() != "torch.constant.str" || !mode ||
      (mode.getValue() != "floor" && mode.getValue() != "trunc"))
    return op->emitError("exact_integer_contract: requires signed i64 floor/trunc division");
  OpBuilder builder(op);
  auto loc = op->getLoc();
  SmallVector<AffineMap> maps;
  for (auto input : {*lhs, *rhs}) {
    if (input.getRank() > result->getRank())
      return op->emitError("exact_integer_contract: division broadcast rank differs");
    SmallVector<AffineExpr> indices;
    auto offset = result->getRank() - input.getRank();
    for (int64_t axis = 0; axis < input.getRank(); ++axis) {
      auto size = input.getDimSize(axis);
      if (size != 1 && size != result->getDimSize(axis + offset))
        return op->emitError("exact_integer_contract: division broadcast shape differs");
      indices.push_back(size == 1 ? builder.getAffineConstantExpr(0)
                                 : builder.getAffineDimExpr(axis + offset));
    }
    maps.push_back(AffineMap::get(result->getRank(), 0, indices, builder.getContext()));
  }
  maps.push_back(AffineMap::getMultiDimIdentityMap(result->getRank(), builder.getContext()));
  Value input = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(0), *lhs);
  Value divisor = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(1), *rhs);
  Value empty = tensor::EmptyOp::create(builder, loc, result->getShape(), builder.getI64Type());
  SmallVector<utils::IteratorType> iterators(result->getRank(), utils::IteratorType::parallel);
  auto lowered = linalg::GenericOp::create(builder, loc, TypeRange{*result},
    ValueRange{input, divisor}, ValueRange{empty}, maps, iterators,
    [&](OpBuilder &nested, Location bodyLoc, ValueRange args) {
      Value quotient;
      if (mode.getValue() == "floor")
        quotient = arith::FloorDivSIOp::create(nested, bodyLoc, args[0], args[1]);
      else quotient = arith::DivSIOp::create(nested, bodyLoc, args[0], args[1]);
      linalg::YieldOp::create(nested, bodyLoc, quotient);
    });
  Value converted = llm2fpga::bridge(builder, loc, "torch_c.from_builtin_tensor",
                                     lowered.getResult(0), op->getResult(0).getType());
  op->getResult(0).replaceAllUsesWith(converted);
  op->erase();
  return success();
}

struct ExactIntegerPass : PassWrapper<ExactIntegerPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ExactIntegerPass)
  StringRef getArgument() const final { return "llm2fpga-lower-exact-integer-ops"; }
  StringRef getDescription() const final { return "Lower constant signed integer shifts and masks"; }
  void getDependentDialects(DialectRegistry &registry) const final {
    registry.insert<arith::ArithDialect, linalg::LinalgDialect, tensor::TensorDialect>();
  }
  void runOnOperation() final {
    if (!getContext().getOrLoadDialect("torch_c")) {
      getOperation().emitError("required Torch conversion dialect is unavailable");
      signalPassFailure(); return;
    }
    SmallVector<Operation *> operators;
    getOperation().walk([&](Operation *op) {
      auto name = op->getName().getStringRef();
      if (name == "torch.operator" || name == "torch.aten.div.Tensor_mode" ||
          name == "torch.aten.remainder.Scalar" || name == "torch.aten.neg")
        operators.push_back(op);
    });
    for (Operation *op : operators) {
      if (op->getName().getStringRef() == "torch.aten.neg") {
        if (failed(lowerNegation(op))) { signalPassFailure(); return; }
        continue;
      }
      if (op->getName().getStringRef() == "torch.aten.div.Tensor_mode") {
        if (failed(lowerDivision(op))) { signalPassFailure(); return; }
        continue;
      }
      auto name = op->getAttrOfType<StringAttr>("name");
      StringRef operatorName = name ? name.getValue() : op->getName().getStringRef();
      bool right = operatorName == "torch.aten.bitwise_right_shift.Tensor_Scalar";
      bool left = operatorName == "torch.aten.bitwise_left_shift.Tensor_Scalar";
      bool mask = operatorName == "torch.aten.bitwise_and.Scalar";
      bool remainder = operatorName == "torch.aten.remainder.Scalar";
      if (!right && !left && !mask && !remainder) continue;
      auto fail = [&](StringRef detail) {
        op->emitError() << "exact_integer_contract: " << detail; signalPassFailure();
      };
      if (op->getNumOperands() != 2 || op->getNumResults() != 1) {
        fail("invalid arity"); return;
      }
      auto input = llm2fpga::staticI64Tensor(op->getOperand(0).getType());
      auto result = llm2fpga::staticI64Tensor(op->getResult(0).getType());
      Operation *constant = op->getOperand(1).getDefiningOp();
      auto amount = constant ? constant->getAttrOfType<IntegerAttr>("value") : IntegerAttr{};
      if (!input || !result || *input != *result || !constant ||
          constant->getName().getStringRef() != "torch.constant.int" || !amount ||
          ((right || left) && (amount.getInt() < 0 || amount.getInt() >= 63)) ||
          (remainder && amount.getInt() <= 0)) {
        fail("requires equal static signed i64 tensors, constant shift in [0,63), or positive remainder divisor"); return;
      }
      OpBuilder builder(op);
      auto loc = op->getLoc();
      Value builtin = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(0), *input);
      Value scalar = arith::ConstantIntOp::create(builder, loc, amount.getInt(), 64);
      Value empty = tensor::EmptyOp::create(builder, loc, input->getShape(), builder.getI64Type());
      auto map = AffineMap::getMultiDimIdentityMap(input->getRank(), builder.getContext());
      SmallVector<AffineMap> maps{map, map};
      SmallVector<utils::IteratorType> iterators(input->getRank(), utils::IteratorType::parallel);
      auto lowered = linalg::GenericOp::create(builder, loc, TypeRange{*result},
        ValueRange{builtin}, ValueRange{empty}, maps, iterators,
        [&](OpBuilder &nested, Location bodyLoc, ValueRange args) {
          Value output;
          if (right) output = arith::ShRSIOp::create(nested, bodyLoc, args[0], scalar);
          else if (left) output = arith::ShLIOp::create(nested, bodyLoc, args[0], scalar);
          else if (mask) output = arith::AndIOp::create(nested, bodyLoc, args[0], scalar);
          else {
            Value residual = arith::RemSIOp::create(nested, bodyLoc, args[0], scalar);
            Value zero = arith::ConstantIntOp::create(nested, bodyLoc, 0, 64);
            Value negative = arith::CmpIOp::create(nested, bodyLoc, arith::CmpIPredicate::slt, residual, zero);
            Value corrected = arith::AddIOp::create(nested, bodyLoc, residual, scalar);
            output = arith::SelectOp::create(nested, bodyLoc, negative, corrected, residual);
          }
          linalg::YieldOp::create(nested, bodyLoc, output);
        });
      Value converted = llm2fpga::bridge(builder, loc, "torch_c.from_builtin_tensor",
                                         lowered.getResult(0), op->getResult(0).getType());
      op->getResult(0).replaceAllUsesWith(converted);
      op->erase();
    }
  }
};
}
void llm2fpga::registerExactIntegerPass() { PassRegistration<ExactIntegerPass>(); }
