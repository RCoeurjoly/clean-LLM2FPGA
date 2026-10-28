#!/usr/bin/env python3
"""Normalize two generated-SV constructs rejected by Yosys/Slang.

The Verilator closure accepts these constructs, but Yosys/Slang requires the
request net to be declared before its instance use and rejects the duplicate
``sqrtOpOut`` declaration emitted in ``divSqrtRecFN_small``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


STD_DIVSQRT = re.compile(r"(?ms)\bmodule\s+std_divSqrtFN\b.*?\bendmodule\b")
RAW_SMALL = re.compile(
    r"(?ms)\bmodule\s+divSqrtRecFN_small\b.*?\bendmodule\b"
)
ONEHOT_ASSERT = re.compile(
    r"(?ms)\s*always_comb\s+begin\s*"
    r"if\s*\(\s*~\s*\$onehot0\s*\(\s*\{.*?\}\s*\)\s*\)\s*begin\s*"
    r"\$fatal\s*\(.*?\)\s*;\s*end\s*end"
)
PACKED_DONE_BUF = re.compile(
    r"(?m)^(?P<indent>\s*)logic\s+done_buf\s*\[1:0\]\s*;"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(source: str) -> tuple[str, dict[str, object]]:
    output = source
    request_repairs = 0
    duplicate_repairs = 0
    assertion_repairs = 0
    packed_done_buf_repairs = 0

    match = STD_DIVSQRT.search(output)
    if match:
        module = match.group(0)
        if "wire llm2fpga_request;" not in module:
            declaration = "    wire llm2fpga_request;\n\n"
            marker = "    // Call HardFloat's"
            if marker in module:
                module = module.replace(marker, declaration + marker, 1)
            elif ".inValid(llm2fpga_request)" in module:
                first_use = module.index(".inValid(llm2fpga_request)")
                line_start = module.rfind("\n", 0, first_use) + 1
                module = module[:line_start] + declaration + module[line_start:]
            if "wire llm2fpga_request =" in module:
                module, count = re.subn(
                    r"wire\s+llm2fpga_request\s*=", "assign llm2fpga_request =", module, count=1
                )
                request_repairs = count
        output = output[: match.start()] + module + output[match.end() :]

    output, assertion_repairs = ONEHOT_ASSERT.subn("\n", output)

    # Calyx's HardFloat wrappers use a two-element unpacked array for a pair
    # of pipeline bits.  Slang accepts it, but Yosys imports every instance as
    # a separate $mem, forcing MEMORY_LIBMAP to solve hundreds of trivial
    # memories.  A packed vector has identical bit-select semantics and is
    # elaborated as ordinary flip-flops.
    output, packed_done_buf_repairs = PACKED_DONE_BUF.subn(
        r"\g<indent>logic [1:0] done_buf;", output
    )

    match = RAW_SMALL.search(output)
    if match:
        module = match.group(0)
        module, duplicate_repairs = re.subn(
            r"\n\s*wire\s+sqrtOpOut\s*;", "", module, count=1
        )
        output = output[: match.start()] + module + output[match.end() :]

    receipt = {
        "schema": "llm2fpga.sv-synthesis-frontend-repair.v1",
        "input_sv_sha256": _sha256(source),
        "output_sv_sha256": _sha256(output),
        "request_declaration_repairs": request_repairs,
        "duplicate_sqrtopout_repairs": duplicate_repairs,
        "assertion_block_repairs": assertion_repairs,
        "packed_done_buf_repairs": packed_done_buf_repairs,
    }
    receipt["repair_count"] = (
        request_repairs + duplicate_repairs + assertion_repairs
        + packed_done_buf_repairs
    )
    return output, receipt


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <input.sv> <output.sv> <receipt.json>", file=sys.stderr)
        return 2
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    output, receipt = normalize(source)
    Path(sys.argv[2]).write_text(output, encoding="utf-8")
    Path(sys.argv[3]).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
