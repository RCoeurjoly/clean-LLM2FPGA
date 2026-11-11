{ lib, runCommand, python, mlir, memory, float-calyx, verifier, yosys }:
let source = lib.fileset.toSource {
  root = ../..;
  fileset = lib.fileset.unions [ ../../tests/test_calyx_arithmetic.py ../../tests/cases ];
};
in runCommand "calyx-arithmetic-equivalence" {
  nativeBuildInputs = [ python ];
  PYTHONDONTWRITEBYTECODE = "1";
  MLIR_OPT = "${mlir}/bin/mlir-opt";
  CALYX_OPT = "${memory}/bin/llm2fpga-calyx-opt";
  CALYX_TRANSLATE = "${memory}/bin/llm2fpga-calyx-translate";
  CALYX = "${float-calyx}/bin/calyx";
  CALYX_LIBRARY = "${float-calyx}/share/calyx";
  FLOAT_CAST = "${float-calyx}/share/calyx/primitives/float/float_cast.sv";
  YOSYS = "${yosys}/bin/yosys";
  IVERILOG = "${verifier}/bin/iverilog";
  VVP = "${verifier}/bin/vvp";
} ''
  python -m unittest discover -s ${source}/tests -p test_calyx_arithmetic.py -v
  touch "$out"
''
