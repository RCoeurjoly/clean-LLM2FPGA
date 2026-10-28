# Standalone graph/controller join downstream of the common authenticated core.
# No reference outputs or historical textual lowering are available here.
{ runCommand, python, torchMlir, core, controller, calyx, yosys }:
let
  compilerSource = core.compilerSource;
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
in runCommand "graph-core-tinystories-1m-ddr-transport" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${compilerSource}:${torchMlir}/${python.sitePackages}:${torchMlir}/${python.sitePackages}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  passthru = { inherit core controller compilerSource; };
} ''
  python ${../scripts/pipeline/export_graph_ddr_transport.py} \
    --core ${core} --controller ${controller}/controller --output "$out" \
    --numerical-contract ${compilerSource}/compiler/contracts/tinystories-1m-fixed-point.json \
    --calyx-library ${calyx}/share/calyx --yosys ${yosys}/bin/yosys \
    --timeout-seconds 1800
''
