{ pkgs, python, baseline, candidate }:
pkgs.runCommand "task6-full16-one-vs-two-rom-resources" {
  nativeBuildInputs = [ python ];
} ''
  set -euo pipefail
  python3 ${../scripts/pipeline/rebuild_task6_bram_resource_comparison.py} ${baseline} ${candidate} "$out"
''
