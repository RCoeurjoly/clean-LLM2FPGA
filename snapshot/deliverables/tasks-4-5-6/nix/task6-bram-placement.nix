{ pkgs, python, synthesis, nextpnr, chipdb, constraints, source }:
pkgs.runCommand "task6-full16-two-rom-placement" {
  nativeBuildInputs = [ python nextpnr ];
} ''
  set -euo pipefail
  export OMP_NUM_THREADS=1
  export PYTHONPATH=${source}
  ulimit -v 16777216
  python3 ${../scripts/pipeline/rebuild_task6_bram_placement.py} \
    ${synthesis} ${nextpnr} ${chipdb} ${constraints} "$out"
''
