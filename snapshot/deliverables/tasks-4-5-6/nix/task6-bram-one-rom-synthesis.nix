{ pkgs, python, yosys, slang, image, source }:
pkgs.runCommand "task6-full16-one-rom-baseline-synthesis" {
  nativeBuildInputs = [ python yosys slang ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 PYTHONPATH=${source}
  ulimit -v 8388608
  python3 ${../scripts/pipeline/rebuild_task6_bram_one_rom_synthesis.py} \
    ${source} ${image} ${yosys} ${slang} \
    ${source}/scripts/pipeline/map_flattened_memory_checkpoints.py "$out"
''
