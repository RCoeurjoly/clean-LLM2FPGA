# Verify the model-data boundary independently of compilation and inference.
{ lib, runCommand, python, modelInputs }:
let
  compilerSource = import ./graph-core-source.nix { inherit lib; };
  tests = lib.fileset.toSource {
    root = ../.;
    fileset = ../tests/test_graph_compile_data.py;
  };
in runCommand "graph-core-input-check" {
  nativeBuildInputs = [ python ];
  PYTHONDONTWRITEBYTECODE = "1";
  PYTHONPATH = compilerSource;
  LLM2FPGA_COMPILE_SOURCE = compilerSource;
  LLM2FPGA_COMPILE_PACKAGE = modelInputs.package;
  LLM2FPGA_COMPILE_SNAPSHOT = modelInputs.snapshot;
  passthru = { inherit modelInputs compilerSource; };
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${tests}/tests -p test_graph_compile_data.py -v \
    > "$out/input-tests.log" 2>&1
''
