# Verification-only gate, separate from compiler inputs and model oracles.
{ lib, runCommand, python3 }:
let
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../scripts/comparison/graph_rtl_runtime.py
      ../scripts/comparison/verify_graph_block_rtl.py
      ../tests/test_graph_rtl_runtime.py
    ];
  };
in runCommand "graph-runtime-verifier-check" {
  nativeBuildInputs = [ python3 ];
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_rtl_runtime.py -v \
    > "$out/tests.log" 2>&1
  python ${source}/scripts/comparison/verify_graph_block_rtl.py --help \
    > "$out/block-verifier-help.txt"
''
