{ pkgs, python, postpass, package, snapshot, source, closureReference }:
pkgs.runCommand "task6-full16-two-rom-static-image" {
  nativeBuildInputs = [ python ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
  test -s ${closureReference}/reference.json
  ulimit -v 8388608
  python3 ${../scripts/pipeline/rebuild_task6_bram_image.py} ${source} ${postpass} ${package} ${snapshot} "$out"
''
