{ pkgs, python, yosys, slang, image, source }:
pkgs.runCommand "task6-full16-two-rom-synthesis" {
  nativeBuildInputs = [ python yosys slang ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONPATH=${source}
  ulimit -v 8388608
  # The checkpoint mapper loads this descriptor by sibling path only when it
  # reaches the immutable distributed-ROM banks. Fail before the costly map.
  test -s ${source}/scripts/pipeline/rom_lutrams_xc7.txt
  python3 ${../scripts/pipeline/rebuild_task6_bram_synthesis.py} \
    ${source} ${image} ${yosys} ${slang} \
    ${../scripts/pipeline/task6_two_rom_mapper.py} "$out"
''
