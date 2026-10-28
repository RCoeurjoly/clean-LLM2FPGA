{ pkgs, python, bitstream }:
pkgs.runCommand "task4-full16-board-evidence-verified" {
  nativeBuildInputs = [ python ];
} ''
  set -euo pipefail
  archive=${../artifacts/task4/full16-board-2026-09-29}
  oracle=${../artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json}
  echo "bbe28dead359698fe9f88a7d3214d795971ad8733b1682135c4168bab9459e1b  $archive/hardware/receipt.json" | sha256sum -c -
  mkdir -p "$out"
  python3 ${../scripts/pipeline/verify_task4_board_archive.py} \
    "$archive" "$oracle" ${bitstream}/receipt.json "$out/verification.json"
''
