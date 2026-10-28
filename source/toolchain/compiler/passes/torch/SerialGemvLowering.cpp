#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"

using namespace mlir;
namespace {
struct SerialGemvPass : PassWrapper<SerialGemvPass, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(SerialGemvPass)
  StringRef getArgument() const final { return "llm2fpga-lower-serial-gemv"; }
  StringRef getDescription() const final { return "Lower typed serial i64 GEMV to a real reduction"; }
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
      if (op->getName().getStringRef() == "torch.operator") operators.push_back(op);
    });
    for (Operation *op : operators) {
      auto name = op->getAttrOfType<StringAttr>("name");
      if (name && name.getValue().starts_with("torch.aten.")) continue;
      auto fail = [&](StringRef detail) {
        op->emitError() << "serial_gemv_contract: " << detail;
        signalPassFailure();
      };
      if (!name || name.getValue() != "torch.llm2fpga.serial_gemv") {
        fail("unknown custom operator"); return;
      }
      if (op->getNumOperands() != 2 || op->getNumResults() != 1 || op->getNumRegions()) {
        fail("invalid arity"); return;
      }
      auto input = llm2fpga::staticI64Tensor(op->getOperand(0).getType());
      auto weight = llm2fpga::staticI64Tensor(op->getOperand(1).getType());
      auto result = llm2fpga::staticI64Tensor(op->getResult(0).getType());
      if (!input || !weight || !result || input->getRank() != 2 ||
          weight->getRank() != 2 || result->getRank() != 2) {
        fail("requires static rank2 signed i64 tensors"); return;
      }
      if (input->getDimSize(1) != weight->getDimSize(1) ||
          result->getDimSize(0) != input->getDimSize(0) ||
          result->getDimSize(1) != weight->getDimSize(0)) {
        fail("shape mismatch"); return;
      }
      OpBuilder builder(op);
      auto loc = op->getLoc();
      Value lhs = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(0), *input);
      Value rhs = llm2fpga::bridge(builder, loc, "torch_c.to_builtin_tensor", op->getOperand(1), *weight);
      Value empty = tensor::EmptyOp::create(builder, loc, result->getShape(), builder.getI64Type());
      Value zero = arith::ConstantIntOp::create(builder, loc, 0, 64);
      auto fill = linalg::FillOp::create(builder, loc, ValueRange{zero}, ValueRange{empty});
      AffineExpr row, output, reduction;
      bindDims(builder.getContext(), row, output, reduction);
      SmallVector<AffineMap> maps{
        AffineMap::get(3, 0, {row, reduction}, builder.getContext()),
        AffineMap::get(3, 0, {output, reduction}, builder.getContext()),
        AffineMap::get(3, 0, {row, output}, builder.getContext())};
      SmallVector<utils::IteratorType> iterators{
        utils::IteratorType::parallel, utils::IteratorType::parallel, utils::IteratorType::reduction};
      auto gemv = linalg::GenericOp::create(builder, loc, TypeRange{*result},
        ValueRange{lhs, rhs}, fill.getResults(), maps, iterators,
        [](OpBuilder &nested, Location bodyLoc, ValueRange args) {
          Value product = arith::MulIOp::create(nested, bodyLoc, args[0], args[1]);
          Value sum = arith::AddIOp::create(nested, bodyLoc, args[2], product);
          linalg::YieldOp::create(nested, bodyLoc, sum);
        });
      // No overflow flags: multiplication/addition wrap at i64. Default loop
      // lowering visits the reduction dimension in ascending order.
      Value converted = llm2fpga::bridge(builder, loc, "torch_c.from_builtin_tensor",
                                         gemv.getResult(0), op->getResult(0).getType());
      op->getResult(0).replaceAllUsesWith(converted);
      op->erase();
    }
  }
};
}
void llm2fpga::registerSerialGemvPass() { PassRegistration<SerialGemvPass>(); }
