# Native controller verifier is separate from compilation source and model data.
{ lib, runCommand, litedramPython, yosys }:
let
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/litedram_export.py
      ../compiler/stages.py
      ../tests/test_graph_litedram_export.py
    ];
  };
in runCommand "graph-litedram-export-check" {
  nativeBuildInputs = [ litedramPython yosys ];
  PYTHONDONTWRITEBYTECODE = "1";
  PYTHONPATH = source;
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_litedram_export.py -v \
    > "$out/native-controller-tests.log" 2>&1
''
