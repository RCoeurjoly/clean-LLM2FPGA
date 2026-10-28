{ pkgs, python, calyx, source }:
pkgs.runCommand "task6-full16-two-rom-compiler-postpass" {
  nativeBuildInputs = [ python calyx ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
  ulimit -v 8388608
  python3 ${../scripts/pipeline/rebuild_task6_bram_postpass.py} ${source} ${calyx} "$out"
''
