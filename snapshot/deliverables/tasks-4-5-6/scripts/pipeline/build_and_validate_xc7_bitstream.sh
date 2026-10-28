#!/usr/bin/env bash
# Convert a routed Kintex-7 FASM image and reject malformed bitstreams.
#
# The validation is intentionally independent of nextpnr: bitread must decode
# at least one non-zero configuration bit from the resulting .bit file.  A
# malformed xc7frames2bit header can otherwise produce a file that programs
# and reports DONE while leaving the user BSCAN path unconfigured.
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 <fasm> <bit-out> <frames-out> <prjxray-db-root> <part-file>" >&2
  exit 2
fi

fasm=$1
bit_out=$2
frames=$3
db_root=$4
part_file=$5

command -v fasm2frames >/dev/null
command -v xc7frames2bit >/dev/null
command -v bitread >/dev/null

fasm2frames --db-root "$db_root" \
  --part "$(basename "$(dirname "$part_file")")" \
  "$fasm" "$frames"
xc7frames2bit --part_file "$part_file" --frm_file "$frames" --output_file "$bit_out"

decoded="${bit_out}.decoded-bits"
bitread --part_file "$part_file" -o "$decoded" "$bit_out" >/dev/null
if [[ ! -s "$decoded" ]]; then
  echo "refusing malformed bitstream: bitread decoded no set configuration bits" >&2
  exit 1
fi

echo "validated bitstream: $bit_out"
echo "decoded configuration bits: $(wc -l < "$decoded")"
