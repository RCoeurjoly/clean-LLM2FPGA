# Source isolation check. This verifier is not a dependency of model compilation.
{ lib, runCommand, python }:
let
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  compilerSource = import ./graph-core-source.nix { inherit lib; };
  tests = lib.fileset.toSource {
    root = ../.;
    fileset = ../tests/test_graph_production_boundary.py;
  };
in runCommand "graph-core-source-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONDONTWRITEBYTECODE = "1";
  LLM2FPGA_COMPILE_SOURCE = compilerSource;
  passthru = { inherit compilerSource; };
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${tests}/tests -p test_graph_production_boundary.py -v \
    > "$out/source-tests.log" 2>&1
''
