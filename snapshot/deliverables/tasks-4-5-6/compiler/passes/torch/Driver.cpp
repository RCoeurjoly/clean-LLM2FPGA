#include "TypedTensor.h"
#include "mlir/InitAllDialects.h"
#include "mlir/InitAllExtensions.h"
#include "mlir/InitAllPasses.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "torch-mlir-c/Registration.h"
int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  mlir::registerAllDialects(registry);
  mlir::registerAllExtensions(registry);
  mlir::registerAllPasses();
  // Both Torch parsing and typed API queries use this shared Torch library.
  mlir::MLIRContext context;
  torchMlirRegisterAllDialects(wrap(&context));
  context.getDialectRegistry().appendTo(registry);
  torchMlirRegisterAllPasses();
  llm2fpga::registerSerialGemvPass();
  llm2fpga::registerGemvEpilogue();
  llm2fpga::registerExactIntegerPass();
  llm2fpga::registerStandardIntegerBoundary();
  llm2fpga::registerTestScalarEntry();
  llm2fpga::registerIntegerMath();
  llm2fpga::registerRestoringSqrtLoops();
  llm2fpga::registerIntegerStrengthReduction();
  llm2fpga::registerInputOrigins();
  llm2fpga::registerBufferReuse();
  llm2fpga::registerPrivateCapacityPool();
  llm2fpga::registerPointwisePingPong();
  llm2fpga::registerSequentialScratchPool();
  llm2fpga::registerFunctionSharing();
  llm2fpga::registerLoopOutlining();
  llm2fpga::registerPrivateStorage();
  llm2fpga::registerProducerSlices();
  llm2fpga::registerPointwiseFusion();
  llm2fpga::registerHorizontalPointwiseFusion();
  llm2fpga::registerGenerationLink();
  llm2fpga::registerGenerationImport();
  return mlir::asMainReturnCode(mlir::MlirOptMain(argc, argv, "LLM2FPGA typed Torch passes\n", registry));
}
