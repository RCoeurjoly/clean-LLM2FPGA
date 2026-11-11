{ lib, runCommand, python, verifier, yosys }:
let source = lib.fileset.toSource {
  root = ../..;
  fileset = lib.fileset.unions [
    ../../tools/bind_memories.py ../../rtl/model_memory.sv ../../tests/test_model_memory.py
  ];
};
in runCommand "model-memory-check" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = source;
  PYTHONDONTWRITEBYTECODE = "1";
  MODEL_MEMORY = "${source}/rtl/model_memory.sv";
  YOSYS = "${yosys}/bin/yosys";
  IVERILOG = "${verifier}/bin/iverilog";
  VVP = "${verifier}/bin/vvp";
} ''
  python -m unittest discover -s ${source}/tests -p test_model_memory.py -v
  touch "$out"
''
