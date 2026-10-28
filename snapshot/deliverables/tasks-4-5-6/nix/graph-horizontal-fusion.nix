# Optional candidate qualification; selection in the shared profile is separate.
{ lib, runCommand, python, torchDriver, torchMlir }:
let
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = ../tests/test_graph_horizontal_fusion.py;
  };
in runCommand "graph-horizontal-fusion-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${torchMlir}/${python.sitePackages}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_horizontal_fusion.py -v \
    > "$out/tests.log" 2>&1
''
