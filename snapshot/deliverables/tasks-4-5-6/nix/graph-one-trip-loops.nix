# Verify that shared scheduling removes one-trip control and preserves effects.
{ lib, runCommand, python, torchDriver, torchMlir }:
let
  pythonEnv = python.withPackages (ps: [ ps.numpy ]);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py ../tests/test_graph_one_trip_loops.py
    ];
  };
in runCommand "graph-one-trip-loops-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${source}:${torchMlir}/${python.sitePackages}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_one_trip_loops.py -v \
    > "$out/tests.log" 2>&1
''
