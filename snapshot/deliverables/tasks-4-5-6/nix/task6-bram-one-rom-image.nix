{ pkgs, python, postpass, image, source }:
pkgs.runCommand "task6-full16-one-rom-baseline-image" {
  nativeBuildInputs = [ python ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONPATH=${source}
  ulimit -v 8388608
  python3 ${../scripts/pipeline/rebuild_task6_bram_one_rom_image.py} ${source} ${postpass} ${image} "$out"
''
