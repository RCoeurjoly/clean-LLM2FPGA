#!/usr/bin/env python3
"""Remove redundant operand widening in Calyx's signed fixed-point multiply.

Calyx emits a WIDTH-bit multiply helper whose signed branch sign-extends both
operands to 2*WIDTH before multiplying.  The helper already stores a 2*WIDTH
result and extracts the fixed-point slice afterwards; widening the operands
turns a 64x64 operation into a 128x128 operation.  This repair keeps the
2*WIDTH result context while leaving the operand widths at WIDTH.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


OLD = """out_tmp <= $signed(
          { {WIDTH{ltmp[WIDTH-1]}}, ltmp} *
          { {WIDTH{rtmp[WIDTH-1]}}, rtmp}
        );"""
OLD_UNSIGNED = "        out_tmp <= ltmp * rtmp;"
OLD_DECL = "  logic [(WIDTH << 1) - 1:0] out_tmp;"
NEW_DECL = """  logic [(WIDTH << 1) - 1:0] out_tmp;
  logic [WIDTH-1:0] left_mag;
  logic [WIDTH-1:0] right_mag;
  logic [(WIDTH << 1) - 1:0] magnitude_product;
  logic [127:0] bounded_product;
  generate if (WIDTH == 64) begin
    llm2fpga_bounded_mul64 #(.SIGNED(SIGNED)) bounded_mul64 (
      .left(ltmp), .right(rtmp), .out(bounded_product));
  end else begin
    assign bounded_product = '0;
  end endgenerate"""
MUL64_MODULE = """module llm2fpga_bounded_mul32 (
    input logic [31:0] left, input logic [31:0] right,
    output logic [63:0] out);
  always_comb out = left * right;
endmodule

module llm2fpga_bounded_mul64 #(parameter SIGNED = 0) (
    input logic [63:0] left, input logic [63:0] right,
    output logic [127:0] out);
  logic [63:0] left_mag, right_mag;
  logic [31:0] left_lo, left_hi, right_lo, right_hi;
  logic [63:0] product_ll, product_lh, product_hl, product_hh;
  logic [127:0] magnitude_out;
  always_comb begin
    left_mag = (SIGNED && left[63]) ? (~left + 1'b1) : left;
    right_mag = (SIGNED && right[63]) ? (~right + 1'b1) : right;
    left_lo = left_mag[31:0]; left_hi = left_mag[63:32];
    right_lo = right_mag[31:0]; right_hi = right_mag[63:32];
    // Keep each partial product's operands explicitly 32 bits.  Otherwise
    // expression sizing in the generated RTL can infer four 64x64 multiplies.
  end
  llm2fpga_bounded_mul32 mul_ll (.left(left_mag[31:0]), .right(right_mag[31:0]), .out(product_ll));
  llm2fpga_bounded_mul32 mul_lh (.left(left_mag[31:0]), .right(right_mag[63:32]), .out(product_lh));
  llm2fpga_bounded_mul32 mul_hl (.left(left_mag[63:32]), .right(right_mag[31:0]), .out(product_hl));
  llm2fpga_bounded_mul32 mul_hh (.left(left_mag[63:32]), .right(right_mag[63:32]), .out(product_hh));
  always_comb begin
    magnitude_out = (product_hh << 64) + ((product_lh + product_hl) << 32) + product_ll;
    out = (SIGNED && (left[63] ^ right[63])) ? (~magnitude_out + 1'b1) : magnitude_out;
  end
endmodule

"""
NEW_SIGNED = """        if (WIDTH == 64)
          out_tmp <= bounded_product;
        else
          out_tmp <= $signed(
            { {WIDTH{ltmp[WIDTH-1]}}, ltmp} *
            { {WIDTH{rtmp[WIDTH-1]}}, rtmp}
          );"""
NEW_UNSIGNED = """        if (WIDTH == 64)
          out_tmp <= bounded_product;
        else
          out_tmp <= ltmp * rtmp;"""


def repair(source: Path, destination: Path, receipt: Path) -> None:
    text = source.read_text(encoding="utf-8")
    matches = text.count(OLD)
    declarations = text.count(OLD_DECL)
    unsigned_matches = text.count(OLD_UNSIGNED)
    if matches != 1 or declarations != 1 or unsigned_matches != 1:
        raise ValueError(
            f"expected one signed/unsigned multiply helper and declaration, found signed={matches}, unsigned={unsigned_matches}, declaration={declarations}"
        )
    repaired = text.replace(OLD_DECL, NEW_DECL).replace(OLD, NEW_SIGNED)
    repaired = repaired.replace(OLD_UNSIGNED, NEW_UNSIGNED)
    marker = "module std_fp_mult_pipe #("
    if marker not in repaired:
        raise ValueError("missing std_fp_mult_pipe module marker")
    repaired = repaired.replace(marker, MUL64_MODULE + marker, 1)
    destination.write_text(repaired, encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "schema": "llm2fpga-calyx-signed-multiply-width-repair-v1",
                "status": "ok",
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "repaired_sha256": hashlib.sha256(repaired.encode()).hexdigest(),
                "replacements": matches + unsigned_matches,
                "operand_width_policy": "unsigned WIDTH magnitudes, signed correction, 2*WIDTH result context",
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    repair(args.source, args.destination, args.receipt)


if __name__ == "__main__":
    main()
