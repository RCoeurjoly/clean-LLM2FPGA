# Executable integer boundary checks use the exact Torch SDK runner.
# This verification derivation is not a compiler input.
{ lib, runCommand, python3, torchDriver, mlir }:
let
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../tests/test_graph_integer_passes.py
      ../compiler/pipeline.py ../compiler/stages.py
    ];
  };
in runCommand "graph-integer-boundary-check" {
  nativeBuildInputs = [ python3 ];
  PYTHONDONTWRITEBYTECODE = "1";
  PYTHONPATH = "${source}";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
  LLM2FPGA_MLIR_RUNNER = "${mlir}/bin/mlir-runner";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_integer_passes.py -v \
    > "$out/tests.log" 2>&1
''
