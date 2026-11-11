#include "circt/Conversion/SCFToCalyx.h"
#include "circt/Transforms/Passes.h"
#include "circt/Dialect/Calyx/CalyxDialect.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/Comb/CombDialect.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/Dialect/ControlFlow/IR/ControlFlowOps.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/Passes.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
void registerPrepareArithmeticPass();
void registerPrepareMathPass();

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<circt::calyx::CalyxDialect, circt::hw::HWDialect,
                  circt::comb::CombDialect, mlir::arith::ArithDialect,
                  mlir::func::FuncDialect, mlir::memref::MemRefDialect,
                  mlir::scf::SCFDialect, mlir::math::MathDialect,
                  mlir::cf::ControlFlowDialect>();
  mlir::registerPass([] { return circt::createSCFToCalyxPass(); });
  mlir::registerSymbolDCEPass();
  mlir::registerCanonicalizerPass();
  mlir::registerPass([] { return circt::createFlattenMemRefPass(); });
  registerPrepareArithmeticPass();
  registerPrepareMathPass();
  return mlir::failed(mlir::MlirOptMain(argc, argv,
      "LLM2FPGA typed Calyx passes\n", registry));
}
