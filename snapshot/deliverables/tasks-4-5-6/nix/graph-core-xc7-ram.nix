# Native RAM mapping of the authenticated graph coarse checkpoint.
# Arithmetic mapping, resource fit, controller glue and board gates are separate.
{ runCommand, python, coarse, yosys }:
runCommand "graph-core-xc7-ram" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${coarse.driverSource}";
  PYTHONDONTWRITEBYTECODE = "1";
  passthru = { inherit coarse; driverSource = coarse.driverSource; };
} ''
  python - <<'PY'
  import os
  from pathlib import Path
  from scripts.pipeline.map_graph_core_xc7 import map_checkpoint
  receipt = map_checkpoint(
      Path('${coarse}'), 'checkpoint.il.gz', Path('${yosys}/bin/yosys'),
      Path(os.environ['out']), phase='ram', compress_outputs=True,
      timeout_seconds=7200)
  print(receipt.identity_sha256)
  PY
''
