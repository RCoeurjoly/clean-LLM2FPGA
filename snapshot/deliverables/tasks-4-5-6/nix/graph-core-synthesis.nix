# Native synthesis export depends on the authenticated graph core, never oracles.
# The authenticated bound core is reused; numerical and FPGA gates are separate.
{ runCommand, python, core, calyx }:
let
  compilerSource = core.compilerSource;
in runCommand "graph-core-tinystories-1m-synthesis-export" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${compilerSource}";
  PYTHONDONTWRITEBYTECODE = "1";
  passthru = { inherit core compilerSource; };
} ''
  python ${../scripts/pipeline/export_graph_core_synthesis.py} \
    --core ${core} --output "$out" \
    --numerical-contract ${compilerSource}/compiler/contracts/tinystories-1m-fixed-point.json \
    --calyx-library ${calyx}/share/calyx
''
