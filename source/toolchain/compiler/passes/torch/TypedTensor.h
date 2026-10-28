#pragma once
#include "mlir/CAPI/IR.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/Builders.h"
#include "torch-mlir-c/TorchTypes.h"
#include <optional>

namespace llm2fpga {
// Use the shared Torch library's typed C API. Its C++ implementations and TypeID
// definitions have hidden visibility in the installed SDK; printing types or
// linking a duplicate Torch dialect into a pass would violate the ABI contract.
inline std::optional<mlir::RankedTensorType> staticI64Tensor(mlir::Type type) {
  auto wrapped = wrap(type);
  if (!torchMlirTypeIsATorchValueTensor(wrapped) ||
      !torchMlirTorchValueTensorTypeHasSizes(wrapped) ||
      !torchMlirTorchValueTensorTypeHasDtype(wrapped))
    return std::nullopt;
  auto dtype = mlir::dyn_cast<mlir::IntegerType>(
      unwrap(torchMlirTorchValueTensorTypeGetDtype(wrapped)));
  if (!dtype || !dtype.isSigned() || dtype.getWidth() != 64)
    return std::nullopt;
  int64_t rank = torchMlirTorchValueTensorTypeGetRank(wrapped);
  if (rank < 0) return std::nullopt;
  llvm::SmallVector<int64_t> sizes(rank);
  torchMlirTorchValueTensorTypeGetSizes(wrapped, sizes.data());
  if (llvm::any_of(sizes, [](int64_t size) { return size <= 0; }))
    return std::nullopt;
  return mlir::RankedTensorType::get(sizes, mlir::IntegerType::get(type.getContext(), 64));
}
inline mlir::Value bridge(mlir::OpBuilder &builder, mlir::Location loc,
                          llvm::StringRef name, mlir::Value value, mlir::Type result) {
  mlir::OperationState state(loc, name);
  state.addOperands(value);
  state.addTypes(result);
  return builder.create(state)->getResult(0);
}
void registerSerialGemvPass();
void registerGemvEpilogue();
void registerExactIntegerPass();
void registerStandardIntegerBoundary();
void registerTestScalarEntry();
void registerIntegerMath();
void registerRestoringSqrtLoops();
void registerIntegerStrengthReduction();
void registerInputOrigins();
void registerBufferReuse();
void registerPrivateCapacityPool();
void registerPointwisePingPong();
void registerSequentialScratchPool();
void registerFunctionSharing();
void registerLoopOutlining();
void registerPrivateStorage();
void registerProducerSlices();
void registerPointwiseFusion();
void registerHorizontalPointwiseFusion();
void registerGenerationLink();
void registerGenerationImport();
} // namespace llm2fpga
