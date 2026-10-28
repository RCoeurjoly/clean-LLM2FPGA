# Native final mapping of the authenticated graph RAM checkpoint.
# Structural qualification does not establish fit, timing or board acceptance.
{ runCommand, python, ram, yosys }:
runCommand "graph-core-xc7-final" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${ram.driverSource}";
  PYTHONDONTWRITEBYTECODE = "1";
  passthru = { inherit ram; driverSource = ram.driverSource; };
} ''
  python - <<'PY'
  import os
  from pathlib import Path
  from scripts.pipeline.map_graph_core_xc7 import map_checkpoint
  receipt = map_checkpoint(
      Path('${ram}'), 'checkpoint.il.gz', Path('${yosys}/bin/yosys'),
      Path(os.environ['out']), phase='final', compress_outputs=True,
      timeout_seconds=7200)
  print(receipt.identity_sha256)
  PY
''
