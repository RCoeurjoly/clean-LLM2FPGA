# Core-only coarse checkpoint. Controller/board glue and final fit are separate.
{ lib, runCommand, python, synthesis, yosys }:
let
  driverSource = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/stages.py
      ../compiler/yosys_pipeline.py
      ../scripts/pipeline/map_graph_core_xc7.py
    ];
  };
in runCommand "graph-core-xc7-coarse" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${driverSource}";
  PYTHONDONTWRITEBYTECODE = "1";
  passthru = { inherit synthesis driverSource; };
} ''
  python - <<'PY'
  import os
  from pathlib import Path
  from scripts.pipeline.map_graph_core_xc7 import coarse_checkpoint
  receipt = coarse_checkpoint(
      Path('${synthesis}/sv'), 'kernel.sv', Path('${yosys}/bin/yosys'),
      Path(os.environ['out']), timeout_seconds=7200)
  print(receipt.identity_sha256)
  PY
''
