#include "circt/Dialect/Calyx/CalyxEmitter.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Tools/mlir-translate/MlirTranslateMain.h"

int main(int argc, char **argv) {
  circt::calyx::registerToCalyxTranslation();
  return mlir::failed(mlir::mlirTranslateMain(argc, argv,
                                           "LLM2FPGA native Calyx export\n"));
}
