# Verification-only fixtures for the native nonduplicating fusion policy.
{ lib, runCommand, python, torchDriver, torchMlir, mlir }:
let
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py
      ../compiler/frontend/capture.py ../compiler/frontend/operator_support.json
      ../tests/test_graph_pointwise_fusion.py
    ];
  };
in runCommand "graph-pointwise-fusion-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${source}:${torchMlir}/${python.sitePackages}:${torchMlir}/${python.sitePackages}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
  LLM2FPGA_MLIR_RUNNER = "${mlir}/bin/mlir-runner";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_pointwise_fusion.py -v \
    > "$out/tests.log" 2>&1
''
