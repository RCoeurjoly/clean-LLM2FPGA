# Verification fixtures are isolated from compiler inputs.
{ lib, runCommand, python, torchDriver, mlir }:
let
  pythonEnv = python.withPackages (ps: [ ps.torch ]);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py
      ../tests/test_graph_producer_slices.py
    ];
  };
in runCommand "graph-producer-slices-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${source}";
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
  LLM2FPGA_MLIR_RUNNER = "${mlir}/bin/mlir-runner";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_producer_slices.py -v \
    > "$out/tests.log" 2>&1
''
