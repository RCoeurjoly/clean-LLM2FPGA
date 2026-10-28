#!/usr/bin/env python3
"""Generate and run the exact fixed-point attention-crossing Calyx gates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCHEMA = "tinystories-1m-fixed-point-attention-crossing-slice-v1"
LN1_ARTIFACT_SCHEMA = "tinystories-1m-fixed-point-attention-ln1-generated-sv-v1"
LN1_SOURCE_MEMORIES = (
    "block_input_q16_16",
    "ln1_gamma_q16_16",
    "ln1_beta_q16_16",
)
LN1_HARDWARE_OWNED_MEMORIES = ("ln1_q16_16",)

CAUSAL_ARTIFACT_SCHEMA = (
    "tinystories-1m-fixed-point-attention-causal-generated-sv-v1"
)
CAUSAL_ARTIFACT_DIRECTORY = Path(
    "/tmp/llm2fpga-tinystories-1m-fixed-point-attention-causal-generated-sv-v1"
)
CAUSAL_SOURCE_MEMORIES = (
    "attention_exp_lut_q1_20",
    "block_input_q16_16",
    "k_input_scale_q8_24",
    "k_output_scale_q8_24",
    "k_weight_codes_i8",
    "k_weight_scale_q8_24",
    "ln1_beta_q16_16",
    "ln1_gamma_q16_16",
    "q_input_scale_q8_24",
    "q_output_scale_q8_24",
    "q_weight_codes_i8",
    "q_weight_scale_q8_24",
    "v_input_scale_q8_24",
    "v_output_scale_q8_24",
    "v_weight_codes_i8",
    "v_weight_scale_q8_24",
)
CAUSAL_COMPUTED_CHECKPOINTS = (
    "ln1_output_q16_16",
    "q_input_codes_i8",
    "q_input_q16_16",
    "q_accumulator_i64",
    "q_post_weight_rescale_bias_q16_16",
    "q_output_codes_i8",
    "q_output_q16_16",
    "k_input_codes_i8",
    "k_input_q16_16",
    "k_accumulator_i64",
    "k_post_weight_rescale_bias_q16_16",
    "k_output_codes_i8",
    "k_output_q16_16",
    "v_input_codes_i8",
    "v_input_q16_16",
    "v_accumulator_i64",
    "v_post_weight_rescale_bias_q16_16",
    "v_output_codes_i8",
    "v_output_q16_16",
    "attention_score_q8",
    "attention_maximum_q8",
    "attention_delta_q8",
    "attention_exp_q1_20",
    "attention_denominator_q1_20",
    "attention_numerator_q17_36",
    "attention_context_heads_q16_16",
    "attention_context_q16_16",
)
CAUSAL_OBSERVED_TENSORS = (
    "ln1_output_q16_16",
    *(
        name
        for prefix in ("q", "k", "v")
        for name in (
            f"{prefix}_input_codes_i8",
            f"{prefix}_input_scale_q8_24",
            f"{prefix}_input_q16_16",
            f"{prefix}_accumulator_i64",
            f"{prefix}_post_weight_rescale_bias_q16_16",
            f"{prefix}_output_codes_i8",
            f"{prefix}_output_scale_q8_24",
            f"{prefix}_output_q16_16",
            f"{prefix}_weight_codes_i8",
            f"{prefix}_weight_scale_q8_24",
        )
    ),
    "attention_score_q8",
    "attention_maximum_q8",
    "attention_delta_q8",
    "attention_exp_q1_20",
    "attention_denominator_q1_20",
    "attention_numerator_q17_36",
    "attention_context_heads_q16_16",
    "attention_context_q16_16",
    "attention_exp_lut_q1_20",
)


@dataclass(frozen=True)
class CalyxArtifact:
    futil: str
    provenance: dict[str, Any]


def _capture_module():
    path = ROOT / "TinyStories/capture_fixed_point_attention_crossing_slice.py"
    spec = importlib.util.spec_from_file_location(
        "fixed_point_attention_crossing_capture_for_calyx", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("authenticated attention fixture verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    """Bind generated artifacts to the complete authenticated fixture."""
    return {
        "schema": fixture["schema"],
        "receipt_sha256": fixture["receipt_sha256"],
        "tensor_fixture_receipt_sha256": fixture[
            "tensor_fixture_receipt_sha256"
        ],
        "identity": fixture["identity"],
        "arithmetic": fixture["arithmetic"],
        "tensors": {
            name: {
                "shape": record["shape"],
                "dtype": record["dtype"],
                "bytes": record["bytes"],
                "canonical_sha256": record["canonical_sha256"],
                "little_endian_int64_sha256": record[
                    "little_endian_int64_sha256"
                ],
            }
            for name, record in sorted(fixture["tensors"].items())
        },
    }


def _checked_ln1_fixture(fixture_path: Path) -> dict[str, Any]:
    """Authenticate the fixture and the exact LayerNorm source-memory ABI."""
    fixture = _capture_module().verify_fixture(fixture_path)
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise ValueError("ln1 fixture schema authority mismatch")
    expected_arithmetic = (
        "truncating_mean_variance_plus_42950_restoring_isqrt_"
        "truncating_division_q16.16_affine"
    )
    if fixture.get("arithmetic", {}).get("layer_norm") != expected_arithmetic:
        raise ValueError("ln1 fixture arithmetic authority mismatch")
    expected = {
        "block_input_q16_16": ([4, 64], 2048),
        "ln1_gamma_q16_16": ([64], 512),
        "ln1_beta_q16_16": ([64], 512),
        "ln1_output_q16_16": ([4, 64], 2048),
    }
    for name, (shape, byte_count) in expected.items():
        record = fixture["tensors"].get(name)
        if (
            not isinstance(record, dict)
            or record.get("shape") != shape
            or record.get("dtype") != "int64"
            or record.get("bytes") != byte_count
        ):
            raise ValueError(f"ln1 fixture memory contract mismatch: {name}")
    return fixture


def _ln1_kernel_futil() -> str:
    """Emit four-row exact fixed LayerNorm with explicit staged state."""
    return '''// Generated exact block-0 ln_1 gate from authenticated fixture authority.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {
  cells {
    @external block_input_q16_16 = seq_mem_d1(64, 256, 8);
    @external ln1_gamma_q16_16 = seq_mem_d1(64, 64, 6);
    @external ln1_beta_q16_16 = seq_mem_d1(64, 64, 6);
    @external ln1_q16_16 = seq_mem_d1(64, 256, 8);

    row_counter = std_reg(3);
    row_lt = std_lt(3);
    increment_row = std_add(3);
    row_pad = std_pad(3, 8);
    row_shift = std_lsh(8);

    column_counter = std_reg(7);
    column_lt = std_lt(7);
    increment_column = std_add(7);
    column_address = std_slice(7, 6);
    column_pad = std_pad(6, 8);
    flat_address = std_add(8);

    row_sum = std_reg(64);
    sum_add = std_sadd(64);
    sum_negative = std_slt(64);
    sum_negate = std_ssub(64);
    sum_absolute = std_mux(64);
    mean_shift = std_rsh(64);
    mean_negate = std_ssub(64);
    mean_select = std_mux(64);
    mean = std_reg(64);

    delta_subtract = std_ssub(64);
    delta_square = std_smult_pipe(64);
    square_sum = std_reg(64);
    square_add = std_add(64);
    variance_shift = std_rsh(64);
    variance_epsilon = std_add(64);
    variance = std_reg(64);

    sqrt_remainder = std_reg(64);
    sqrt_root = std_reg(64);
    sqrt_bit = std_reg(64);
    sqrt_counter = std_reg(6);
    sqrt_lt = std_lt(6);
    sqrt_increment = std_add(6);
    sqrt_candidate = std_add(64);
    sqrt_take = std_ge(64);
    sqrt_subtract = std_sub(64);
    sqrt_root_shift = std_rsh(64);
    sqrt_root_add_bit = std_add(64);
    sqrt_remainder_select = std_mux(64);
    sqrt_root_select = std_mux(64);
    sqrt_bit_shift = std_rsh(64);

    numerator_shift = std_lsh(64);
    numerator_negative = std_slt(64);
    numerator = std_reg(64);
    numerator_negative_latch = std_reg(1);
    numerator_negate = std_ssub(64);
    numerator_absolute = std_mux(64);
    normalize_divide = std_div_pipe(64);
    quotient_negate = std_ssub(64);
    normalized_select = std_mux(64);
    affine_multiply = std_smult_pipe(64);
    affine_shift = std_srsh(64);
    affine_add = std_sadd(64);
  }
  wires {
    row_pad.in = row_counter.out;
    row_shift.left = row_pad.out;
    row_shift.right = 8'd6;
    column_address.in = column_counter.out;
    column_pad.in = column_address.out;
    flat_address.left = row_shift.out;
    flat_address.right = column_pad.out;

    group init_row {
      row_counter.in = 3'd0;
      row_counter.write_en = 1'd1;
      init_row[done] = row_counter.done;
    }
    group init_column {
      column_counter.in = 7'd0;
      column_counter.write_en = 1'd1;
      init_column[done] = column_counter.done;
    }
    group increment_column_counter {
      increment_column.left = column_counter.out;
      increment_column.right = 7'd1;
      column_counter.in = increment_column.out;
      column_counter.write_en = 1'd1;
      increment_column_counter[done] = column_counter.done;
    }
    group increment_row_counter {
      increment_row.left = row_counter.out;
      increment_row.right = 3'd1;
      row_counter.in = increment_row.out;
      row_counter.write_en = 1'd1;
      increment_row_counter[done] = row_counter.done;
    }
    comb group row_condition {
      row_lt.left = row_counter.out;
      row_lt.right = 3'd4;
    }
    comb group column_condition {
      column_lt.left = column_counter.out;
      column_lt.right = 7'd64;
    }

    group clear_row_sum {
      row_sum.in = 64'd0;
      row_sum.write_en = 1'd1;
      clear_row_sum[done] = row_sum.done;
    }
    group read_block_input {
      block_input_q16_16.addr0 = flat_address.out;
      block_input_q16_16.content_en = 1'd1;
      read_block_input[done] = block_input_q16_16.done;
    }
    group accumulate_row_sum {
      sum_add.left = row_sum.out;
      sum_add.right = block_input_q16_16.read_data;
      row_sum.in = sum_add.out;
      row_sum.write_en = 1'd1;
      accumulate_row_sum[done] = row_sum.done;
    }
    group latch_mean {
      sum_negative.left = row_sum.out;
      sum_negative.right = 64'd0;
      sum_negate.left = 64'd0;
      sum_negate.right = row_sum.out;
      sum_absolute.cond = sum_negative.out;
      sum_absolute.tru = sum_negate.out;
      sum_absolute.fal = row_sum.out;
      mean_shift.left = sum_absolute.out;
      mean_shift.right = 64'd6;
      mean_negate.left = 64'd0;
      mean_negate.right = mean_shift.out;
      mean_select.cond = sum_negative.out;
      mean_select.tru = mean_negate.out;
      mean_select.fal = mean_shift.out;
      mean.in = mean_select.out;
      mean.write_en = 1'd1;
      latch_mean[done] = mean.done;
    }

    group clear_square_sum {
      square_sum.in = 64'd0;
      square_sum.write_en = 1'd1;
      clear_square_sum[done] = square_sum.done;
    }
    group multiply_delta_square {
      delta_subtract.left = block_input_q16_16.read_data;
      delta_subtract.right = mean.out;
      delta_square.left = delta_subtract.out;
      delta_square.right = delta_subtract.out;
      delta_square.go = 1'd1;
      multiply_delta_square[done] = delta_square.done;
    }
    group accumulate_delta_square {
      square_add.left = square_sum.out;
      square_add.right = delta_square.out;
      square_sum.in = square_add.out;
      square_sum.write_en = 1'd1;
      accumulate_delta_square[done] = square_sum.done;
    }
    group latch_variance {
      variance_shift.left = square_sum.out;
      variance_shift.right = 64'd6;
      variance_epsilon.left = variance_shift.out;
      variance_epsilon.right = 64'd42950;
      variance.in = variance_epsilon.out;
      variance.write_en = 1'd1;
      latch_variance[done] = variance.done;
    }

    group init_sqrt {
      sqrt_remainder.in = variance.out;
      sqrt_remainder.write_en = 1'd1;
      sqrt_root.in = 64'd0;
      sqrt_root.write_en = 1'd1;
      sqrt_bit.in = 64'd4611686018427387904;
      sqrt_bit.write_en = 1'd1;
      sqrt_counter.in = 6'd0;
      sqrt_counter.write_en = 1'd1;
      init_sqrt[done] = (sqrt_remainder.done & sqrt_root.done & sqrt_bit.done & sqrt_counter.done) ? 1'd1;
    }
    group sqrt_step {
      sqrt_candidate.left = sqrt_root.out;
      sqrt_candidate.right = sqrt_bit.out;
      sqrt_take.left = sqrt_remainder.out;
      sqrt_take.right = sqrt_candidate.out;
      sqrt_subtract.left = sqrt_remainder.out;
      sqrt_subtract.right = sqrt_candidate.out;
      sqrt_root_shift.left = sqrt_root.out;
      sqrt_root_shift.right = 64'd1;
      sqrt_root_add_bit.left = sqrt_root_shift.out;
      sqrt_root_add_bit.right = sqrt_bit.out;
      sqrt_remainder_select.cond = sqrt_take.out;
      sqrt_remainder_select.tru = sqrt_subtract.out;
      sqrt_remainder_select.fal = sqrt_remainder.out;
      sqrt_root_select.cond = sqrt_take.out;
      sqrt_root_select.tru = sqrt_root_add_bit.out;
      sqrt_root_select.fal = sqrt_root_shift.out;
      sqrt_bit_shift.left = sqrt_bit.out;
      sqrt_bit_shift.right = 64'd2;
      sqrt_remainder.in = sqrt_remainder_select.out;
      sqrt_remainder.write_en = 1'd1;
      sqrt_root.in = sqrt_root_select.out;
      sqrt_root.write_en = 1'd1;
      sqrt_bit.in = sqrt_bit_shift.out;
      sqrt_bit.write_en = 1'd1;
      sqrt_increment.left = sqrt_counter.out;
      sqrt_increment.right = 6'd1;
      sqrt_counter.in = sqrt_increment.out;
      sqrt_counter.write_en = 1'd1;
      sqrt_step[done] = (sqrt_remainder.done & sqrt_root.done & sqrt_bit.done & sqrt_counter.done) ? 1'd1;
    }
    comb group sqrt_condition {
      sqrt_lt.left = sqrt_counter.out;
      sqrt_lt.right = 6'd32;
    }

    group read_output_operands {
      block_input_q16_16.addr0 = flat_address.out;
      block_input_q16_16.content_en = 1'd1;
      ln1_gamma_q16_16.addr0 = column_address.out;
      ln1_gamma_q16_16.content_en = 1'd1;
      ln1_beta_q16_16.addr0 = column_address.out;
      ln1_beta_q16_16.content_en = 1'd1;
      read_output_operands[done] = (block_input_q16_16.done & ln1_gamma_q16_16.done & ln1_beta_q16_16.done) ? 1'd1;
    }
    group latch_normalize_numerator {
      delta_subtract.left = block_input_q16_16.read_data;
      delta_subtract.right = mean.out;
      numerator_shift.left = delta_subtract.out;
      numerator_shift.right = 64'd16;
      numerator_negative.left = numerator_shift.out;
      numerator_negative.right = 64'd0;
      numerator.in = numerator_shift.out;
      numerator.write_en = 1'd1;
      numerator_negative_latch.in = numerator_negative.out;
      numerator_negative_latch.write_en = 1'd1;
      latch_normalize_numerator[done] = (numerator.done & numerator_negative_latch.done) ? 1'd1;
    }
    group divide_normalized {
      numerator_negate.left = 64'd0;
      numerator_negate.right = numerator.out;
      numerator_absolute.cond = numerator_negative_latch.out;
      numerator_absolute.tru = numerator_negate.out;
      numerator_absolute.fal = numerator.out;
      normalize_divide.left = numerator_absolute.out;
      normalize_divide.right = sqrt_root.out;
      normalize_divide.go = 1'd1;
      divide_normalized[done] = normalize_divide.done;
    }
    group multiply_affine {
      quotient_negate.left = 64'd0;
      quotient_negate.right = normalize_divide.out_quotient;
      normalized_select.cond = numerator_negative_latch.out;
      normalized_select.tru = quotient_negate.out;
      normalized_select.fal = normalize_divide.out_quotient;
      affine_multiply.left = normalized_select.out;
      affine_multiply.right = ln1_gamma_q16_16.read_data;
      affine_multiply.go = 1'd1;
      multiply_affine[done] = affine_multiply.done;
    }
    group write_ln1 {
      affine_shift.left = affine_multiply.out;
      affine_shift.right = 64'd16;
      affine_add.left = affine_shift.out;
      affine_add.right = ln1_beta_q16_16.read_data;
      ln1_q16_16.addr0 = flat_address.out;
      ln1_q16_16.content_en = 1'd1;
      ln1_q16_16.write_data = affine_add.out;
      ln1_q16_16.write_en = 1'd1;
      write_ln1[done] = ln1_q16_16.done;
    }
  }
  control {
    seq {
      init_row;
      while row_lt.out with row_condition {
        seq {
          clear_row_sum;
          init_column;
          while column_lt.out with column_condition {
            seq { read_block_input; accumulate_row_sum; increment_column_counter; }
          }
          latch_mean;
          clear_square_sum;
          init_column;
          while column_lt.out with column_condition {
            seq { read_block_input; multiply_delta_square; accumulate_delta_square; increment_column_counter; }
          }
          latch_variance;
          init_sqrt;
          while sqrt_lt.out with sqrt_condition { sqrt_step; }
          init_column;
          while column_lt.out with column_condition {
            seq {
              read_output_operands;
              latch_normalize_numerator;
              divide_normalized;
              multiply_affine;
              write_ln1;
              increment_column_counter;
            }
          }
          increment_row_counter;
        }
      }
    }
  }
}
'''


def generate_ln1_kernel(fixture_path: Path) -> CalyxArtifact:
    """Authenticate the attention fixture and generate the ln_1 Calyx main."""
    fixture = _checked_ln1_fixture(fixture_path)
    futil = _ln1_kernel_futil()
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": LN1_ARTIFACT_SCHEMA,
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": _fixture_authority(fixture),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "host_preload_memories": list(LN1_SOURCE_MEMORIES),
            "hardware_owned_memories": list(LN1_HARDWARE_OWNED_MEMORIES),
            "calyx_disabled_passes": ["cell-share"],
            "memory_shapes": {
                "block_input_q16_16": [256, 64],
                "ln1_gamma_q16_16": [64, 64],
                "ln1_beta_q16_16": [64, 64],
                "ln1_q16_16": [256, 64],
            },
            "kernel": {
                "rows": 4,
                "width": 64,
                "mean": "signed_truncating_divide_by_64",
                "variance": "ascending_delta_square_sum_divide_by_64_plus_42950",
                "sqrt": "32_step_unsigned_restoring_floor",
                "normalize": "signed_truncating_division_q16_16",
                "affine": "signed_64_product_arithmetic_shift_16_plus_beta",
            },
        },
    )


def _cpp_values(values: list[int]) -> str:
    return ", ".join(str(int(value)) for value in values)


def _generated_ln1_harness(fixture: dict[str, Any]) -> str:
    """Preload only the authenticated block input and ln_1 affine parameters."""
    block_input = [
        value
        for row in fixture["tensors"]["block_input_q16_16"]["values"]
        for value in row
    ]
    gamma = fixture["tensors"]["ln1_gamma_q16_16"]["values"]
    beta = fixture["tensors"]["ln1_beta_q16_16"]["values"]
    return f'''// Generated harness: preload only block input and ln_1 gamma/beta.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

static const std::int64_t kBlockInput[256] = {{{_cpp_values(block_input)}}};
static const std::int64_t kLn1Gamma[64] = {{{_cpp_values(gamma)}}};
static const std::int64_t kLn1Beta[64] = {{{_cpp_values(beta)}}};

static void tick(Vmain& model) {{
  model.clk = 0;
  model.eval();
  model.clk = 1;
  model.eval();
}}

int main(int argc, char** argv) {{
  Verilated::commandArgs(argc, argv);
  Vmain model;
  auto* root = model.rootp;
  for (unsigned i = 0; i < 256; ++i) {{
    root->main__DOT__block_input_q16_16__DOT__mem[i] =
        static_cast<std::uint64_t>(kBlockInput[i]);
    root->main__DOT__ln1_q16_16__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 64; ++i) {{
    root->main__DOT__ln1_gamma_q16_16__DOT__mem[i] =
        static_cast<std::uint64_t>(kLn1Gamma[i]);
    root->main__DOT__ln1_beta_q16_16__DOT__mem[i] =
        static_cast<std::uint64_t>(kLn1Beta[i]);
  }}
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 1000000) {{
    tick(model);
    ++cycles;
  }}
  const bool complete = model.done && cycles > 256;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"ln1_q16_16\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<std::int64_t>(
        root->main__DOT__ln1_q16_16__DOT__mem[i]);
  }}
  std::cout << "],\\\"cycles\\\":" << cycles << "}}\\n";
  return complete ? 0 : 1;
}}
'''


def _calyx_install() -> Path:
    completed = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", ".#calyx"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"unable to build pinned Calyx tool: {completed.stderr.strip()}"
        )
    paths = [Path(line) for line in completed.stdout.splitlines() if line]
    if len(paths) != 1 or not (paths[0] / "bin/calyx").is_file():
        raise RuntimeError("pinned Calyx package did not yield one executable installation")
    return paths[0]


def _little_endian_i64_sha256(values: list[int]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(int(value).to_bytes(8, byteorder="little", signed=True))
    return digest.hexdigest()


def run_ln1_sv(
    artifact: CalyxArtifact, fixture_path: Path
) -> dict[str, object]:
    """Re-authenticate, compile, and observe all 256 hardware-owned ln_1 words."""
    if artifact.provenance.get("generated") != LN1_ARTIFACT_SCHEMA:
        raise ValueError("unrecognized generated-SV ln1 artifact provenance")
    fixture = _checked_ln1_fixture(fixture_path)
    if (
        artifact.provenance.get("fixture_receipt_sha256")
        != fixture["receipt_sha256"]
        or artifact.provenance.get("authority") != _fixture_authority(fixture)
    ):
        raise ValueError("generated-SV ln1 fixture authority mismatch")
    if artifact.provenance.get("futil_sha256") != hashlib.sha256(
        artifact.futil.encode("utf-8")
    ).hexdigest():
        raise ValueError("generated-SV ln1 Futil authority mismatch")

    artifact_dir = Path(tempfile.mkdtemp(prefix="fixed-point-attention-ln1-sv-"))
    futil_path = artifact_dir / "ln1.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness = _generated_ln1_harness(fixture)
    harness_path.write_text(harness, encoding="utf-8")

    calyx = _calyx_install()
    calyx_command = [
        str(calyx / "bin/calyx"),
        str(futil_path),
        "-l",
        str(calyx / "share/calyx"),
        "-d",
        "cell-share",
        "-b",
        "verilog",
        "-o",
        str(sv_path),
    ]
    calyx_run = subprocess.run(
        calyx_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if calyx_run.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"Calyx-to-SV failed: {calyx_run.stderr.strip()}")

    verilator_dir = artifact_dir / "verilator"
    verilator_run = subprocess.run(
        [
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "--top-module",
            "main",
            "--public-flat-rw",
            "--Mdir",
            str(verilator_dir),
            "-CFLAGS",
            "-std=c++17",
            "-o",
            "fixed_point_attention_ln1_harness",
            str(sv_path),
            str(harness_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    executable = verilator_dir / "fixed_point_attention_ln1_harness"
    if verilator_run.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"Verilator build failed: {verilator_run.stderr.strip()}")

    simulated = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if simulated.returncode != 0:
        raise RuntimeError(
            "generated-SV ln1 harness failed: "
            f"{simulated.stdout.strip()} {simulated.stderr.strip()}"
        )
    try:
        observed = json.loads(
            next(
                line
                for line in reversed(simulated.stdout.splitlines())
                if line.startswith("{")
            )
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError(
            f"generated-SV ln1 harness did not emit JSON: {simulated.stdout!r}"
        ) from error
    flat = observed.get("ln1_q16_16")
    if (
        observed.get("status") != "ok"
        or not isinstance(flat, list)
        or len(flat) != 256
        or not all(isinstance(value, int) for value in flat)
        or not isinstance(observed.get("cycles"), int)
    ):
        raise RuntimeError(f"generated-SV ln1 observation is invalid: {observed}")

    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
        "harness_sha256": hashlib.sha256(harness.encode("utf-8")).hexdigest(),
        "sv_sha256": hashlib.sha256(sv_path.read_bytes()).hexdigest(),
    }
    artifact.provenance["calyx_command"] = calyx_command
    rows = [flat[index : index + 64] for index in range(0, 256, 64)]
    return {
        "ln1_q16_16": rows,
        "little_endian_int64_sha256": _little_endian_i64_sha256(flat),
        "cycles": observed["cycles"],
    }


def _mlp_lowerer():
    """Load the compiler-owned, already-verified exact GEMV/QDQ generators."""
    path = ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    spec = importlib.util.spec_from_file_location(
        "fixed_point_mlp_shared_phase_generators", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("shared exact fixed-point phase generators unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _flatten_values(value: object) -> list[int]:
    if isinstance(value, list):
        flattened: list[int] = []
        for item in value:
            flattened.extend(_flatten_values(item))
        return flattened
    if isinstance(value, int):
        return [value]
    raise ValueError("fixture tensor contains a non-integer value")


def _flatten_tensor(fixture: dict[str, Any], name: str) -> list[int]:
    return _flatten_values(fixture["tensors"][name]["values"])


def _checked_causal_fixture(fixture_path: Path) -> dict[str, Any]:
    """Authenticate the complete source-to-causal-context memory contract."""
    fixture = _capture_module().verify_fixture(fixture_path)
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise ValueError("causal-attention fixture schema authority mismatch")
    if fixture.get("slice", {}).get("causal_valid_key_counts") != [1, 2, 3, 4]:
        raise ValueError("causal-attention legal-slot authority mismatch")
    expected_arithmetic = {
        "activation_codes": "signed_int8_saturated",
        "activation_qdq": "signed_int8_saturated_nearest_ties_away_from_zero",
        "attention_division": "signed_half_denominator_correction_then_truncating_division",
        "attention_exp": "delta_clamp_minus4096_to_0_q8_exact_zero_q1.20_lut",
        "attention_score": "sum_q16.16_products_arithmetic_shift_right_24",
        "gemv": "ascending_input_index_signed_int64_twos_complement_wrap",
        "layer_norm": (
            "truncating_mean_variance_plus_42950_restoring_isqrt_"
            "truncating_division_q16.16_affine"
        ),
        "post_weight_rescale": (
            "signed_magnitude_half_up_shift_32_plus_optional_q16.16_bias"
        ),
        "scale_format": "unsigned_q8.24_int64_tensor",
        "value_format": "signed_q16.16_int64_tensor",
    }
    if fixture.get("arithmetic") != expected_arithmetic:
        raise ValueError("causal-attention arithmetic authority mismatch")

    expected_shapes: dict[str, list[int]] = {
        "block_input_q16_16": [4, 64],
        "ln1_gamma_q16_16": [64],
        "ln1_beta_q16_16": [64],
        "ln1_output_q16_16": [4, 64],
        "attention_score_q8": [4, 16, 4],
        "attention_maximum_q8": [4, 16],
        "attention_delta_q8": [4, 16, 4],
        "attention_exp_q1_20": [4, 16, 4],
        "attention_denominator_q1_20": [4, 16],
        "attention_numerator_q17_36": [4, 16, 4],
        "attention_context_heads_q16_16": [4, 16, 4],
        "attention_context_q16_16": [4, 64],
        "attention_exp_lut_q1_20": [4096],
    }
    for prefix in ("q", "k", "v"):
        expected_shapes.update(
            {
                f"{prefix}_input_codes_i8": [4, 64],
                f"{prefix}_input_scale_q8_24": [64],
                f"{prefix}_input_q16_16": [4, 64],
                f"{prefix}_accumulator_i64": [4, 64],
                f"{prefix}_post_weight_rescale_bias_q16_16": [4, 64],
                f"{prefix}_output_codes_i8": [4, 64],
                f"{prefix}_output_scale_q8_24": [64],
                f"{prefix}_output_q16_16": [4, 64],
                f"{prefix}_weight_codes_i8": [64, 64],
                f"{prefix}_weight_scale_q8_24": [64],
            }
        )
    for name, shape in expected_shapes.items():
        record = fixture["tensors"].get(name)
        count = 1
        for dimension in shape:
            count *= dimension
        if (
            not isinstance(record, dict)
            or record.get("shape") != shape
            or record.get("dtype") != "int64"
            or record.get("bytes") != count * 8
            or record.get("fixture_receipt_sha256")
            != fixture["tensor_fixture_receipt_sha256"]
        ):
            raise ValueError(f"causal-attention memory contract mismatch: {name}")
    return fixture


def _ln1_composed_phase() -> tuple[list[str], list[str], str]:
    """Reuse the proven generated LayerNorm logic inside a larger `main`."""
    shared = _mlp_lowerer()
    cells_text, wires_text, control_text = shared._component_sections(
        _ln1_kernel_futil()
    )
    external_lines = (
        "    @external block_input_q16_16 = seq_mem_d1(64, 256, 8);\n",
        "    @external ln1_gamma_q16_16 = seq_mem_d1(64, 64, 6);\n",
        "    @external ln1_beta_q16_16 = seq_mem_d1(64, 64, 6);\n",
        "    @external ln1_q16_16 = seq_mem_d1(64, 256, 8);\n",
    )
    for line in external_lines:
        if line not in cells_text:
            raise ValueError("proven ln1 memory ABI changed")
        cells_text = cells_text.replace(line, "", 1)
    cells_text = re.sub(r"\bln1_q16_16\b", "ln1_output_q16_16", cells_text)
    wires_text = re.sub(r"\bln1_q16_16\b", "ln1_output_q16_16", wires_text)
    return (
        [line.strip() for line in cells_text.splitlines() if line.strip()],
        [wires_text.strip()],
        control_text.strip(),
    )


def _initialize_zero_bias_phase() -> tuple[list[str], list[str], str]:
    cells = [
        "zero_bias_counter = std_reg(7);",
        "zero_bias_lt = std_lt(7);",
        "zero_bias_increment = std_add(7);",
        "zero_bias_address = std_slice(7, 6);",
    ]
    wires = ["""zero_bias_address.in = zero_bias_counter.out;
    group zero_bias_init_counter {
      zero_bias_counter.in = 7'd0;
      zero_bias_counter.write_en = 1'd1;
      zero_bias_init_counter[done] = zero_bias_counter.done;
    }
    group zero_bias_write {
      zero_bias_q16_16.addr0 = zero_bias_address.out;
      zero_bias_q16_16.content_en = 1'd1;
      zero_bias_q16_16.write_data = 64'd0;
      zero_bias_q16_16.write_en = 1'd1;
      zero_bias_write[done] = zero_bias_q16_16.done;
    }
    group zero_bias_increment_counter {
      zero_bias_increment.left = zero_bias_counter.out;
      zero_bias_increment.right = 7'd1;
      zero_bias_counter.in = zero_bias_increment.out;
      zero_bias_counter.write_en = 1'd1;
      zero_bias_increment_counter[done] = zero_bias_counter.done;
    }
    comb group zero_bias_condition {
      zero_bias_lt.left = zero_bias_counter.out;
      zero_bias_lt.right = 7'd64;
    }"""]
    control = """      zero_bias_init_counter;
      while zero_bias_lt.out with zero_bias_condition {
        seq { zero_bias_write; zero_bias_increment_counter; }
      }"""
    return cells, wires, control


def _initialize_causal_slots_phase() -> tuple[list[str], list[str], str]:
    """Hardware-write the provenance-defined zero value into future slots."""
    cells = [
        "causal_zero_counter = std_reg(9);",
        "causal_zero_lt = std_lt(9);",
        "causal_zero_increment = std_add(9);",
        "causal_zero_address = std_slice(9, 8);",
    ]
    wires = ["""causal_zero_address.in = causal_zero_counter.out;
    group causal_zero_init_counter {
      causal_zero_counter.in = 9'd0;
      causal_zero_counter.write_en = 1'd1;
      causal_zero_init_counter[done] = causal_zero_counter.done;
    }
    group causal_zero_write {
      attention_score_q8.addr0 = causal_zero_address.out;
      attention_score_q8.content_en = 1'd1;
      attention_score_q8.write_data = 64'd0;
      attention_score_q8.write_en = 1'd1;
      attention_delta_q8.addr0 = causal_zero_address.out;
      attention_delta_q8.content_en = 1'd1;
      attention_delta_q8.write_data = 64'd0;
      attention_delta_q8.write_en = 1'd1;
      attention_exp_q1_20.addr0 = causal_zero_address.out;
      attention_exp_q1_20.content_en = 1'd1;
      attention_exp_q1_20.write_data = 64'd0;
      attention_exp_q1_20.write_en = 1'd1;
      causal_zero_write[done] = (attention_score_q8.done & attention_delta_q8.done & attention_exp_q1_20.done) ? 1'd1;
    }
    group causal_zero_increment_counter {
      causal_zero_increment.left = causal_zero_counter.out;
      causal_zero_increment.right = 9'd1;
      causal_zero_counter.in = causal_zero_increment.out;
      causal_zero_counter.write_en = 1'd1;
      causal_zero_increment_counter[done] = causal_zero_counter.done;
    }
    comb group causal_zero_condition {
      causal_zero_lt.left = causal_zero_counter.out;
      causal_zero_lt.right = 9'd256;
    }"""]
    control = """      causal_zero_init_counter;
      while causal_zero_lt.out with causal_zero_condition {
        seq { causal_zero_write; causal_zero_increment_counter; }
      }"""
    return cells, wires, control


def _causal_attention_phase() -> tuple[list[str], list[str], str]:
    """Emit exact four-row, sixteen-head causal score/softmax/context logic."""
    cells = [
        "causal_row = std_reg(3);",
        "causal_head = std_reg(5);",
        "causal_key = std_reg(3);",
        "causal_lane = std_reg(3);",
        "causal_row_lt = std_lt(3);",
        "causal_head_lt = std_lt(5);",
        "causal_key_le = std_le(3);",
        "causal_lane_lt = std_lt(3);",
        "causal_row_increment = std_add(3);",
        "causal_head_increment = std_add(5);",
        "causal_key_increment = std_add(3);",
        "causal_lane_increment = std_add(3);",
        "causal_row_pad8 = std_pad(3, 8);",
        "causal_row_shift6 = std_lsh(8);",
        "causal_key_pad8 = std_pad(3, 8);",
        "causal_key_shift6 = std_lsh(8);",
        "causal_head_pad8 = std_pad(5, 8);",
        "causal_head_shift2 = std_lsh(8);",
        "causal_lane_pad8 = std_pad(3, 8);",
        "causal_query_head_lane = std_add(8);",
        "causal_query_address = std_add(8);",
        "causal_key_address = std_add(8);",
        "causal_slot_head_key = std_add(8);",
        "causal_slot_address = std_add(8);",
        "causal_row_pad6 = std_pad(3, 6);",
        "causal_row_shift4 = std_lsh(6);",
        "causal_head_pad6 = std_pad(5, 6);",
        "causal_row_head_address = std_add(6);",
        "causal_dot = std_reg(64);",
        "causal_product = std_smult_pipe(64);",
        "causal_dot_add = std_sadd(64);",
        "causal_score_shift = std_srsh(64);",
        "causal_score = std_reg(64);",
        "causal_maximum = std_reg(64);",
        "causal_score_gt_max = std_sgt(64);",
        "causal_max_select = std_mux(64);",
        "causal_negative_4096 = std_ssub(64);",
        "causal_delta_subtract = std_ssub(64);",
        "causal_delta_lt_min = std_slt(64);",
        "causal_delta_clamp = std_mux(64);",
        "causal_delta = std_reg(64);",
        "causal_lut_sum = std_sadd(64);",
        "causal_lut_gt_last = std_sgt(64);",
        "causal_lut_clamp = std_mux(64);",
        "causal_lut_address = std_slice(64, 12);",
        "causal_delta_is_zero = std_eq(64);",
        "causal_probability_select = std_mux(64);",
        "causal_denominator = std_reg(64);",
        "causal_denominator_add = std_add(64);",
        "causal_numerator = std_reg(64);",
        "causal_value_product = std_smult_pipe(64);",
        "causal_numerator_add = std_sadd(64);",
        "causal_denominator_half = std_rsh(64);",
        "causal_numerator_negative = std_slt(64);",
        "causal_negative_half = std_ssub(64);",
        "causal_correction = std_mux(64);",
        "causal_corrected_add = std_sadd(64);",
        "causal_corrected = std_reg(64);",
        "causal_corrected_negative = std_reg(1);",
        "causal_corrected_negate = std_ssub(64);",
        "causal_corrected_abs = std_mux(64);",
        "causal_divide = std_div_pipe(64);",
        "causal_quotient_negate = std_ssub(64);",
        "causal_context_select = std_mux(64);",
    ]
    wires = ["""causal_row_pad8.in = causal_row.out;
    causal_row_shift6.left = causal_row_pad8.out;
    causal_row_shift6.right = 8'd6;
    causal_key_pad8.in = causal_key.out;
    causal_key_shift6.left = causal_key_pad8.out;
    causal_key_shift6.right = 8'd6;
    causal_head_pad8.in = causal_head.out;
    causal_head_shift2.left = causal_head_pad8.out;
    causal_head_shift2.right = 8'd2;
    causal_lane_pad8.in = causal_lane.out;
    causal_query_head_lane.left = causal_head_shift2.out;
    causal_query_head_lane.right = causal_lane_pad8.out;
    causal_query_address.left = causal_row_shift6.out;
    causal_query_address.right = causal_query_head_lane.out;
    causal_key_address.left = causal_key_shift6.out;
    causal_key_address.right = causal_query_head_lane.out;
    causal_slot_head_key.left = causal_head_shift2.out;
    causal_slot_head_key.right = causal_key_pad8.out;
    causal_slot_address.left = causal_row_shift6.out;
    causal_slot_address.right = causal_slot_head_key.out;
    causal_row_pad6.in = causal_row.out;
    causal_row_shift4.left = causal_row_pad6.out;
    causal_row_shift4.right = 6'd4;
    causal_head_pad6.in = causal_head.out;
    causal_row_head_address.left = causal_row_shift4.out;
    causal_row_head_address.right = causal_head_pad6.out;

    group causal_init_row {
      causal_row.in = 3'd0;
      causal_row.write_en = 1'd1;
      causal_init_row[done] = causal_row.done;
    }
    group causal_increment_row {
      causal_row_increment.left = causal_row.out;
      causal_row_increment.right = 3'd1;
      causal_row.in = causal_row_increment.out;
      causal_row.write_en = 1'd1;
      causal_increment_row[done] = causal_row.done;
    }
    group causal_init_head {
      causal_head.in = 5'd0;
      causal_head.write_en = 1'd1;
      causal_init_head[done] = causal_head.done;
    }
    group causal_increment_head {
      causal_head_increment.left = causal_head.out;
      causal_head_increment.right = 5'd1;
      causal_head.in = causal_head_increment.out;
      causal_head.write_en = 1'd1;
      causal_increment_head[done] = causal_head.done;
    }
    group causal_increment_key {
      causal_key_increment.left = causal_key.out;
      causal_key_increment.right = 3'd1;
      causal_key.in = causal_key_increment.out;
      causal_key.write_en = 1'd1;
      causal_increment_key[done] = causal_key.done;
    }
    group causal_increment_lane {
      causal_lane_increment.left = causal_lane.out;
      causal_lane_increment.right = 3'd1;
      causal_lane.in = causal_lane_increment.out;
      causal_lane.write_en = 1'd1;
      causal_increment_lane[done] = causal_lane.done;
    }
    comb group causal_row_condition {
      causal_row_lt.left = causal_row.out;
      causal_row_lt.right = 3'd4;
    }
    comb group causal_head_condition {
      causal_head_lt.left = causal_head.out;
      causal_head_lt.right = 5'd16;
    }
    comb group causal_key_condition {
      causal_key_le.left = causal_key.out;
      causal_key_le.right = causal_row.out;
    }
    comb group causal_lane_condition {
      causal_lane_lt.left = causal_lane.out;
      causal_lane_lt.right = 3'd4;
    }

    group causal_init_score_state {
      causal_maximum.in = 64'd9223372036854775808;
      causal_maximum.write_en = 1'd1;
      causal_key.in = 3'd0;
      causal_key.write_en = 1'd1;
      causal_init_score_state[done] = (causal_maximum.done & causal_key.done) ? 1'd1;
    }
    group causal_init_dot_and_lane {
      causal_dot.in = 64'd0;
      causal_dot.write_en = 1'd1;
      causal_lane.in = 3'd0;
      causal_lane.write_en = 1'd1;
      causal_init_dot_and_lane[done] = (causal_dot.done & causal_lane.done) ? 1'd1;
    }
    group causal_read_qk {
      q_output_q16_16.addr0 = causal_query_address.out;
      q_output_q16_16.content_en = 1'd1;
      k_output_q16_16.addr0 = causal_key_address.out;
      k_output_q16_16.content_en = 1'd1;
      causal_read_qk[done] = (q_output_q16_16.done & k_output_q16_16.done) ? 1'd1;
    }
    group causal_multiply_qk {
      causal_product.left = q_output_q16_16.read_data;
      causal_product.right = k_output_q16_16.read_data;
      causal_product.go = 1'd1;
      causal_multiply_qk[done] = causal_product.done;
    }
    group causal_accumulate_dot {
      causal_dot_add.left = causal_dot.out;
      causal_dot_add.right = causal_product.out;
      causal_dot.in = causal_dot_add.out;
      causal_dot.write_en = 1'd1;
      causal_accumulate_dot[done] = causal_dot.done;
    }
    group causal_latch_score {
      causal_score_shift.left = causal_dot.out;
      causal_score_shift.right = 64'd24;
      causal_score.in = causal_score_shift.out;
      causal_score.write_en = 1'd1;
      causal_latch_score[done] = causal_score.done;
    }
    group causal_write_score_update_max {
      causal_score_gt_max.left = causal_score.out;
      causal_score_gt_max.right = causal_maximum.out;
      causal_max_select.cond = causal_score_gt_max.out;
      causal_max_select.tru = causal_score.out;
      causal_max_select.fal = causal_maximum.out;
      causal_maximum.in = causal_max_select.out;
      causal_maximum.write_en = 1'd1;
      attention_score_q8.addr0 = causal_slot_address.out;
      attention_score_q8.content_en = 1'd1;
      attention_score_q8.write_data = causal_score.out;
      attention_score_q8.write_en = 1'd1;
      causal_write_score_update_max[done] = (causal_maximum.done & attention_score_q8.done) ? 1'd1;
    }
    group causal_write_maximum {
      attention_maximum_q8.addr0 = causal_row_head_address.out;
      attention_maximum_q8.content_en = 1'd1;
      attention_maximum_q8.write_data = causal_maximum.out;
      attention_maximum_q8.write_en = 1'd1;
      causal_write_maximum[done] = attention_maximum_q8.done;
    }

    group causal_init_probability_state {
      causal_key.in = 3'd0;
      causal_key.write_en = 1'd1;
      causal_denominator.in = 64'd0;
      causal_denominator.write_en = 1'd1;
      causal_init_probability_state[done] = (causal_key.done & causal_denominator.done) ? 1'd1;
    }
    group causal_read_score {
      attention_score_q8.addr0 = causal_slot_address.out;
      attention_score_q8.content_en = 1'd1;
      causal_read_score[done] = attention_score_q8.done;
    }
    group causal_latch_delta {
      causal_negative_4096.left = 64'd0;
      causal_negative_4096.right = 64'd4096;
      causal_delta_subtract.left = attention_score_q8.read_data;
      causal_delta_subtract.right = causal_maximum.out;
      causal_delta_lt_min.left = causal_delta_subtract.out;
      causal_delta_lt_min.right = causal_negative_4096.out;
      causal_delta_clamp.cond = causal_delta_lt_min.out;
      causal_delta_clamp.tru = causal_negative_4096.out;
      causal_delta_clamp.fal = causal_delta_subtract.out;
      causal_delta.in = causal_delta_clamp.out;
      causal_delta.write_en = 1'd1;
      attention_delta_q8.addr0 = causal_slot_address.out;
      attention_delta_q8.content_en = 1'd1;
      attention_delta_q8.write_data = causal_delta_clamp.out;
      attention_delta_q8.write_en = 1'd1;
      causal_latch_delta[done] = (causal_delta.done & attention_delta_q8.done) ? 1'd1;
    }
    group causal_read_exp_lut {
      causal_lut_sum.left = causal_delta.out;
      causal_lut_sum.right = 64'd4096;
      causal_lut_gt_last.left = causal_lut_sum.out;
      causal_lut_gt_last.right = 64'd4095;
      causal_lut_clamp.cond = causal_lut_gt_last.out;
      causal_lut_clamp.tru = 64'd4095;
      causal_lut_clamp.fal = causal_lut_sum.out;
      causal_lut_address.in = causal_lut_clamp.out;
      attention_exp_lut_q1_20.addr0 = causal_lut_address.out;
      attention_exp_lut_q1_20.content_en = 1'd1;
      causal_read_exp_lut[done] = attention_exp_lut_q1_20.done;
    }
    group causal_write_exp_accumulate_denominator {
      causal_delta_is_zero.left = causal_delta.out;
      causal_delta_is_zero.right = 64'd0;
      causal_probability_select.cond = causal_delta_is_zero.out;
      causal_probability_select.tru = 64'd1048576;
      causal_probability_select.fal = attention_exp_lut_q1_20.read_data;
      causal_denominator_add.left = causal_denominator.out;
      causal_denominator_add.right = causal_probability_select.out;
      causal_denominator.in = causal_denominator_add.out;
      causal_denominator.write_en = 1'd1;
      attention_exp_q1_20.addr0 = causal_slot_address.out;
      attention_exp_q1_20.content_en = 1'd1;
      attention_exp_q1_20.write_data = causal_probability_select.out;
      attention_exp_q1_20.write_en = 1'd1;
      causal_write_exp_accumulate_denominator[done] = (causal_denominator.done & attention_exp_q1_20.done) ? 1'd1;
    }
    group causal_write_denominator {
      attention_denominator_q1_20.addr0 = causal_row_head_address.out;
      attention_denominator_q1_20.content_en = 1'd1;
      attention_denominator_q1_20.write_data = causal_denominator.out;
      attention_denominator_q1_20.write_en = 1'd1;
      causal_write_denominator[done] = attention_denominator_q1_20.done;
    }

    group causal_init_numerator_lane {
      causal_lane.in = 3'd0;
      causal_lane.write_en = 1'd1;
      causal_init_numerator_lane[done] = causal_lane.done;
    }
    group causal_init_numerator_key {
      causal_key.in = 3'd0;
      causal_key.write_en = 1'd1;
      causal_numerator.in = 64'd0;
      causal_numerator.write_en = 1'd1;
      causal_init_numerator_key[done] = (causal_key.done & causal_numerator.done) ? 1'd1;
    }
    group causal_read_probability_value {
      attention_exp_q1_20.addr0 = causal_slot_address.out;
      attention_exp_q1_20.content_en = 1'd1;
      v_output_q16_16.addr0 = causal_key_address.out;
      v_output_q16_16.content_en = 1'd1;
      causal_read_probability_value[done] = (attention_exp_q1_20.done & v_output_q16_16.done) ? 1'd1;
    }
    group causal_multiply_probability_value {
      causal_value_product.left = attention_exp_q1_20.read_data;
      causal_value_product.right = v_output_q16_16.read_data;
      causal_value_product.go = 1'd1;
      causal_multiply_probability_value[done] = causal_value_product.done;
    }
    group causal_accumulate_numerator {
      causal_numerator_add.left = causal_numerator.out;
      causal_numerator_add.right = causal_value_product.out;
      causal_numerator.in = causal_numerator_add.out;
      causal_numerator.write_en = 1'd1;
      causal_accumulate_numerator[done] = causal_numerator.done;
    }
    group causal_write_numerator_latch_corrected {
      causal_denominator_half.left = causal_denominator.out;
      causal_denominator_half.right = 64'd1;
      causal_numerator_negative.left = causal_numerator.out;
      causal_numerator_negative.right = 64'd0;
      causal_negative_half.left = 64'd0;
      causal_negative_half.right = causal_denominator_half.out;
      causal_correction.cond = causal_numerator_negative.out;
      causal_correction.tru = causal_negative_half.out;
      causal_correction.fal = causal_denominator_half.out;
      causal_corrected_add.left = causal_numerator.out;
      causal_corrected_add.right = causal_correction.out;
      causal_corrected.in = causal_corrected_add.out;
      causal_corrected.write_en = 1'd1;
      causal_corrected_negative.in = causal_numerator_negative.out;
      causal_corrected_negative.write_en = 1'd1;
      attention_numerator_q17_36.addr0 = causal_query_address.out;
      attention_numerator_q17_36.content_en = 1'd1;
      attention_numerator_q17_36.write_data = causal_numerator.out;
      attention_numerator_q17_36.write_en = 1'd1;
      causal_write_numerator_latch_corrected[done] = (causal_corrected.done & causal_corrected_negative.done & attention_numerator_q17_36.done) ? 1'd1;
    }
    group causal_divide_context {
      causal_corrected_negate.left = 64'd0;
      causal_corrected_negate.right = causal_corrected.out;
      causal_corrected_abs.cond = causal_corrected_negative.out;
      causal_corrected_abs.tru = causal_corrected_negate.out;
      causal_corrected_abs.fal = causal_corrected.out;
      causal_divide.left = causal_corrected_abs.out;
      causal_divide.right = causal_denominator.out;
      causal_divide.go = 1'd1;
      causal_divide_context[done] = causal_divide.done;
    }
    group causal_write_context {
      causal_quotient_negate.left = 64'd0;
      causal_quotient_negate.right = causal_divide.out_quotient;
      causal_context_select.cond = causal_corrected_negative.out;
      causal_context_select.tru = causal_quotient_negate.out;
      causal_context_select.fal = causal_divide.out_quotient;
      attention_context_heads_q16_16.addr0 = causal_query_address.out;
      attention_context_heads_q16_16.content_en = 1'd1;
      attention_context_heads_q16_16.write_data = causal_context_select.out;
      attention_context_heads_q16_16.write_en = 1'd1;
      attention_context_q16_16.addr0 = causal_query_address.out;
      attention_context_q16_16.content_en = 1'd1;
      attention_context_q16_16.write_data = causal_context_select.out;
      attention_context_q16_16.write_en = 1'd1;
      causal_write_context[done] = (attention_context_heads_q16_16.done & attention_context_q16_16.done) ? 1'd1;
    }"""]
    control = """      causal_init_row;
      while causal_row_lt.out with causal_row_condition {
        seq {
          causal_init_head;
          while causal_head_lt.out with causal_head_condition {
            seq {
              causal_init_score_state;
              while causal_key_le.out with causal_key_condition {
                seq {
                  causal_init_dot_and_lane;
                  while causal_lane_lt.out with causal_lane_condition {
                    seq { causal_read_qk; causal_multiply_qk; causal_accumulate_dot; causal_increment_lane; }
                  }
                  causal_latch_score;
                  causal_write_score_update_max;
                  causal_increment_key;
                }
              }
              causal_write_maximum;
              causal_init_probability_state;
              while causal_key_le.out with causal_key_condition {
                seq {
                  causal_read_score;
                  causal_latch_delta;
                  causal_read_exp_lut;
                  causal_write_exp_accumulate_denominator;
                  causal_increment_key;
                }
              }
              causal_write_denominator;
              causal_init_numerator_lane;
              while causal_lane_lt.out with causal_lane_condition {
                seq {
                  causal_init_numerator_key;
                  while causal_key_le.out with causal_key_condition {
                    seq {
                      causal_read_probability_value;
                      causal_multiply_probability_value;
                      causal_accumulate_numerator;
                      causal_increment_key;
                    }
                  }
                  causal_write_numerator_latch_corrected;
                  causal_divide_context;
                  causal_write_context;
                  causal_increment_lane;
                }
              }
              causal_increment_head;
            }
          }
          causal_increment_row;
        }
      }"""
    return cells, wires, control


def _causal_kernel_futil() -> str:
    shared = _mlp_lowerer()
    memory_cells = [
        "@external attention_exp_lut_q1_20 = seq_mem_d1(64, 4096, 12);",
        "@external block_input_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external ln1_gamma_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external ln1_beta_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external ln1_output_q16_16 = seq_mem_d1(64, 256, 8);",
        "zero_bias_q16_16 = seq_mem_d1(64, 64, 6);",
    ]
    for prefix in ("q", "k", "v"):
        memory_cells.extend(
            [
                f"@external {prefix}_input_codes_i8 = seq_mem_d1(8, 256, 8);",
                f"@external {prefix}_input_scale_q8_24 = seq_mem_d1(64, 64, 6);",
                f"@external {prefix}_input_q16_16 = seq_mem_d1(64, 256, 8);",
                f"@external {prefix}_weight_codes_i8 = seq_mem_d1(8, 4096, 12);",
                f"@external {prefix}_weight_scale_q8_24 = seq_mem_d1(64, 64, 6);",
                f"@external {prefix}_accumulator_i64 = seq_mem_d1(64, 256, 8);",
                f"@external {prefix}_post_weight_rescale_bias_q16_16 = seq_mem_d1(64, 256, 8);",
                f"@external {prefix}_output_scale_q8_24 = seq_mem_d1(64, 64, 6);",
                f"@external {prefix}_output_codes_i8 = seq_mem_d1(8, 256, 8);",
                f"@external {prefix}_output_q16_16 = seq_mem_d1(64, 256, 8);",
            ]
        )
    memory_cells.extend(
        [
            "@external attention_score_q8 = seq_mem_d1(64, 256, 8);",
            "@external attention_maximum_q8 = seq_mem_d1(64, 64, 6);",
            "@external attention_delta_q8 = seq_mem_d1(64, 256, 8);",
            "@external attention_exp_q1_20 = seq_mem_d1(64, 256, 8);",
            "@external attention_denominator_q1_20 = seq_mem_d1(64, 64, 6);",
            "@external attention_numerator_q17_36 = seq_mem_d1(64, 256, 8);",
            "@external attention_context_heads_q16_16 = seq_mem_d1(64, 256, 8);",
            "@external attention_context_q16_16 = seq_mem_d1(64, 256, 8);",
        ]
    )
    phases: list[tuple[list[str], list[str], str]] = [
        _ln1_composed_phase(),
        _initialize_zero_bias_phase(),
    ]
    for prefix in ("q", "k", "v"):
        phases.extend(
            [
                shared._activation_qdq_phase(
                    f"{prefix}_input_qdq",
                    "ln1_output_q16_16",
                    f"{prefix}_input_scale_q8_24",
                    f"{prefix}_input_codes_i8",
                    f"{prefix}_input_q16_16",
                    256,
                    64,
                ),
                shared._gemv_phase(
                    f"{prefix}_gemv",
                    f"{prefix}_input_codes_i8",
                    f"{prefix}_input_scale_q8_24",
                    f"{prefix}_weight_codes_i8",
                    f"{prefix}_accumulator_i64",
                    4,
                    64,
                    64,
                ),
                shared._requantize_phase(
                    f"{prefix}_requant",
                    f"{prefix}_accumulator_i64",
                    f"{prefix}_weight_scale_q8_24",
                    "zero_bias_q16_16",
                    f"{prefix}_output_scale_q8_24",
                    f"{prefix}_post_weight_rescale_bias_q16_16",
                    f"{prefix}_output_codes_i8",
                    f"{prefix}_output_q16_16",
                    256,
                    64,
                ),
            ]
        )
    phases.extend([_initialize_causal_slots_phase(), _causal_attention_phase()])
    phase_cells = [cell for cells, _, _ in phases for cell in cells]
    phase_wires = [wire for _, wires, _ in phases for wire in wires]
    phase_controls = [control for _, _, control in phases]
    futil = f'''// Generated exact TinyStories-1M block-0 causal-attention diagnostic.
// All LayerNorm, projection, and causal checkpoints are hardware-produced.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
{shared._indent_lines(memory_cells + phase_cells, 4)}
  }}
  wires {{
{shared._indent_lines(phase_wires, 4)}
  }}
  control {{
    seq {{
{shared._indent_lines(phase_controls, 0)}
    }}
  }}
}}
'''
    if len(re.findall(r"(?m)^component\s+main\b", futil)) != 1:
        raise ValueError("causal-attention generator did not emit exactly one main")
    return futil


def generate_causal_attention_kernel(fixture_path: Path) -> CalyxArtifact:
    """Generate the one-component source-to-causal-context diagnostic gate."""
    fixture = _checked_causal_fixture(fixture_path)
    futil = _causal_kernel_futil()
    shared_path = ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": CAUSAL_ARTIFACT_SCHEMA,
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "tensor_fixture_receipt_sha256": fixture[
                "tensor_fixture_receipt_sha256"
            ],
            "authority": _fixture_authority(fixture),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "shared_phase_generator_sha256": hashlib.sha256(
                shared_path.read_bytes()
            ).hexdigest(),
            "component_count": 1,
            "host_preload_memories": list(CAUSAL_SOURCE_MEMORIES),
            "hardware_owned_memories": list(CAUSAL_COMPUTED_CHECKPOINTS),
            "calyx_disabled_passes": ["cell-share"],
            "legal_causal_key_counts": [1, 2, 3, 4],
            "future_slot_convention": "hardware_written_zero",
            "host_intermediate": False,
        },
    )


def _cpp_print_memory(name: str, count: int, *, signed_i8: bool) -> str:
    expression = f"root->main__DOT__{name}__DOT__mem[i]"
    if signed_i8:
        expression = (
            "static_cast<std::int64_t>(static_cast<std::int8_t>("
            + expression
            + "))"
        )
    else:
        expression = f"static_cast<std::int64_t>({expression})"
    return f'''  std::cout << ",\\\"{name}\\\":[";
  for (unsigned i = 0; i < {count}; ++i) {{
    if (i) std::cout << ',';
    std::cout << {expression};
  }}
  std::cout << ']';'''


def _generated_causal_harness(fixture: dict[str, Any]) -> str:
    """Preload source memories, zero computed memories, and expose raw state."""
    declarations: list[str] = []
    preload_groups: list[str] = []
    cpp_names: list[str] = []
    for index, name in enumerate(CAUSAL_SOURCE_MEMORIES):
        values = _flatten_tensor(fixture, name)
        cpp_name = f"kSource{index:02d}"
        cpp_names.append(cpp_name)
        declarations.append(
            f"// {name}\nstatic const std::int64_t {cpp_name}[{len(values)}] = "
            f"{{{_cpp_values(values)}}};"
        )
        cast = "std::uint8_t" if name.endswith("codes_i8") else "std::uint64_t"
        preload_groups.append(
            f"  for (unsigned i = 0; i < {len(values)}; ++i)\n"
            f"    root->main__DOT__{name}__DOT__mem[i] = "
            f"static_cast<{cast}>({cpp_name}[i]);"
        )

    checkpoint_counts = {
        name: fixture["tensors"][name]["bytes"] // 8
        for name in CAUSAL_COMPUTED_CHECKPOINTS
    }
    zero_groups = [
        f"  for (unsigned i = 0; i < {count}; ++i)\n"
        f"    root->main__DOT__{name}__DOT__mem[i] = 0;"
        for name, count in checkpoint_counts.items()
    ]
    print_groups = [
        _cpp_print_memory(
            name,
            fixture["tensors"][name]["bytes"] // 8,
            signed_i8=name.endswith("codes_i8"),
        )
        for name in CAUSAL_OBSERVED_TENSORS
    ]
    harness = f'''// Generated source-only causal-attention diagnostic harness.
// Static arrays below correspond exactly to immutable authenticated sources.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

{chr(10).join(declarations)}

static void tick(Vmain& model) {{
  model.clk = 0;
  model.eval();
  model.clk = 1;
  model.eval();
}}

int main(int argc, char** argv) {{
  Verilated::commandArgs(argc, argv);
  Vmain model;
  auto* root = model.rootp;
{chr(10).join(preload_groups)}
{chr(10).join(zero_groups)}
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 10000000) {{
    tick(model);
    ++cycles;
  }}
  const bool complete = model.done && cycles > 256;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "timeout")
            << "\\\",\\\"cycles\\\":" << cycles;
{chr(10).join(print_groups)}
  std::cout << "}}\\n";
  return complete ? 0 : 1;
}}
'''
    found_arrays = re.findall(
        r"^static const std::int64_t\s+(kSource[0-9]+)\[",
        harness,
        re.MULTILINE,
    )
    if found_arrays != cpp_names:
        raise ValueError("causal-attention harness source preload whitelist mismatch")
    for name in CAUSAL_COMPUTED_CHECKPOINTS:
        assignment = rf"main__DOT__{re.escape(name)}__DOT__mem\[i\]\s*=\s*([^;]+);"
        if re.findall(assignment, harness) != ["0"]:
            raise ValueError(f"host writes causal-attention checkpoint memory: {name}")
    if "kExpected" in harness:
        raise ValueError("causal-attention harness contains an expected-output oracle")
    return harness


def _reshape_flat(values: list[int], shape: list[int]) -> object:
    if len(shape) == 1:
        if len(values) != shape[0]:
            raise ValueError("observed tensor length does not match shape")
        return values
    stride = 1
    for dimension in shape[1:]:
        stride *= dimension
    if len(values) != shape[0] * stride:
        raise ValueError("observed tensor length does not match shape")
    return [
        _reshape_flat(values[index * stride : (index + 1) * stride], shape[1:])
        for index in range(shape[0])
    ]


def _require_causal_checkpoint(
    name: str, values: list[int], fixture: dict[str, Any]
) -> str:
    expected = _flatten_tensor(fixture, name)
    if len(values) != len(expected):
        raise RuntimeError(
            f"generated-SV causal {name} length mismatch: "
            f"{len(values)} != {len(expected)}"
        )
    for index, (actual, wanted) in enumerate(zip(values, expected, strict=True)):
        if actual != wanted:
            raise RuntimeError(
                f"generated-SV causal {name} mismatch at checkpoint {index}: "
                f"observed {actual}, expected {wanted}"
            )
    digest = _little_endian_i64_sha256(values)
    if digest != fixture["tensors"][name]["little_endian_int64_sha256"]:
        raise RuntimeError(f"generated-SV causal {name} hash mismatch")
    return digest


def run_causal_attention_sv(
    artifact: CalyxArtifact, fixture_path: Path
) -> dict[str, object]:
    """Compile once and verify every Q/K/V and causal-attention observation."""
    if artifact.provenance.get("generated") != CAUSAL_ARTIFACT_SCHEMA:
        raise ValueError("unrecognized generated-SV causal-attention artifact")
    fixture = _checked_causal_fixture(fixture_path)
    shared_path = ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    if (
        artifact.provenance.get("fixture_receipt_sha256")
        != fixture["receipt_sha256"]
        or artifact.provenance.get("tensor_fixture_receipt_sha256")
        != fixture["tensor_fixture_receipt_sha256"]
        or artifact.provenance.get("authority") != _fixture_authority(fixture)
        or artifact.provenance.get("shared_phase_generator_sha256")
        != hashlib.sha256(shared_path.read_bytes()).hexdigest()
    ):
        raise ValueError("generated-SV causal-attention fixture authority mismatch")
    if artifact.provenance.get("futil_sha256") != hashlib.sha256(
        artifact.futil.encode("utf-8")
    ).hexdigest():
        raise ValueError("generated-SV causal-attention Futil authority mismatch")
    for name in CAUSAL_COMPUTED_CHECKPOINTS:
        if f"{name}.write_en = 1'd1" not in artifact.futil:
            raise ValueError(f"generated-SV causal checkpoint is not hardware-written: {name}")

    artifact_dir = CAUSAL_ARTIFACT_DIRECTORY
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "causal-attention.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness = _generated_causal_harness(fixture)
    harness_path.write_text(harness, encoding="utf-8")

    calyx = _calyx_install()
    calyx_command = [
        str(calyx / "bin/calyx"),
        str(futil_path),
        "-l",
        str(calyx / "share/calyx"),
        "-d",
        "cell-share",
        "-b",
        "verilog",
        "-o",
        str(sv_path),
    ]
    compiled = subprocess.run(
        calyx_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if compiled.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(
            f"causal-attention Calyx-to-SV failed: {compiled.stderr.strip()}"
        )

    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_attention_causal_harness"
    verilator_command = [
        "verilator",
        "--cc",
        "--exe",
        "--build",
        "--top-module",
        "main",
        "--public-flat-rw",
        "--Mdir",
        str(verilator_dir),
        "-CFLAGS",
        "-std=c++17",
        "-o",
        executable.name,
        str(sv_path),
        str(harness_path),
    ]
    verilated = subprocess.run(
        verilator_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if verilated.returncode != 0 or not executable.is_file():
        raise RuntimeError(
            f"causal-attention Verilator build failed: {verilated.stderr.strip()}"
        )
    simulated = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if simulated.returncode != 0:
        raise RuntimeError(
            "generated-SV causal-attention harness failed: "
            f"{simulated.stdout.strip()} {simulated.stderr.strip()}"
        )
    try:
        raw = json.loads(
            next(
                line
                for line in reversed(simulated.stdout.splitlines())
                if line.startswith("{")
            )
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError(
            "generated-SV causal-attention harness did not emit JSON: "
            f"{simulated.stdout!r}"
        ) from error
    if raw.get("status") != "ok" or not isinstance(raw.get("cycles"), int):
        raise RuntimeError(
            f"generated-SV causal-attention observation is invalid: {raw}"
        )

    result: dict[str, object] = {"cycles": raw["cycles"]}
    hashes: dict[str, str] = {}
    for name in CAUSAL_OBSERVED_TENSORS:
        values = raw.get(name)
        if not isinstance(values, list) or not all(
            isinstance(value, int) for value in values
        ):
            raise RuntimeError(f"generated-SV causal checkpoint invalid: {name}")
        hashes[name] = _require_causal_checkpoint(name, values, fixture)
        result[name] = _reshape_flat(values, fixture["tensors"][name]["shape"])
    result["little_endian_int64_sha256"] = hashes

    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
        "futil_sha256": hashlib.sha256(futil_path.read_bytes()).hexdigest(),
        "sv_sha256": hashlib.sha256(sv_path.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(harness.encode("utf-8")).hexdigest(),
    }
    artifact.provenance["calyx_command"] = calyx_command
    artifact.provenance["verilator_command"] = verilator_command
    artifact.provenance["observed_little_endian_int64_sha256"] = hashes
    artifact.provenance["cycles"] = raw["cycles"]
    return result


COMPOSED_ATTENTION_SCHEMA = (
    "tinystories-1m-fixed-point-attention-crossing-generated-sv-v1"
)
COMPOSED_ATTENTION_ARTIFACT_DIRECTORY = Path(
    "/tmp/llm2fpga-tinystories-1m-fixed-point-attention-crossing-generated-sv-v1"
)
COMPOSED_ATTENTION_SOURCE_MEMORIES = tuple(
    sorted(
        (
            *CAUSAL_SOURCE_MEMORIES,
            "c_fc_input_scale_q8_24",
            "ln2_beta_q16_16",
            "ln2_gamma_q16_16",
            "out_bias_q16_16",
            "out_input_scale_q8_24",
            "out_output_scale_q8_24",
            "out_weight_codes_i8",
            "out_weight_scale_q8_24",
        )
    )
)
COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS = (
    *CAUSAL_COMPUTED_CHECKPOINTS,
    "out_input_codes_i8",
    "out_input_q16_16",
    "out_accumulator_i64",
    "out_post_weight_rescale_bias_q16_16",
    "out_output_codes_i8",
    "out_output_q16_16",
    "attention_residual_q16_16",
    "ln2_output_q16_16",
    "c_fc_input_codes_i8",
    "c_fc_input_q16_16",
)
COMPOSED_ATTENTION_OBSERVED_TENSORS = tuple(
    sorted(
        (
            *COMPOSED_ATTENTION_SOURCE_MEMORIES,
            *COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS,
        )
    )
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checked_composed_attention_fixtures(
    fixture_path: Path, mlp_fixture_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authenticate the full attention fixture and its linked MLP authority."""
    attention = _checked_causal_fixture(fixture_path)
    mlp = _mlp_lowerer()._checked_composed_fixture(mlp_fixture_path)
    additional_shapes = {
        "out_input_codes_i8": [4, 64],
        "out_input_scale_q8_24": [64],
        "out_input_q16_16": [4, 64],
        "out_accumulator_i64": [4, 64],
        "out_post_weight_rescale_bias_q16_16": [4, 64],
        "out_output_codes_i8": [4, 64],
        "out_output_scale_q8_24": [64],
        "out_output_q16_16": [4, 64],
        "out_weight_codes_i8": [64, 64],
        "out_weight_scale_q8_24": [64],
        "out_bias_q16_16": [64],
        "attention_residual_q16_16": [4, 64],
        "ln2_gamma_q16_16": [64],
        "ln2_beta_q16_16": [64],
        "ln2_output_q16_16": [4, 64],
        "c_fc_input_codes_i8": [4, 64],
        "c_fc_input_scale_q8_24": [64],
        "c_fc_input_q16_16": [4, 64],
    }
    for name, shape in additional_shapes.items():
        record = attention["tensors"].get(name)
        count = 1
        for dimension in shape:
            count *= dimension
        if (
            not isinstance(record, dict)
            or record.get("shape") != shape
            or record.get("dtype") != "int64"
            or record.get("bytes") != count * 8
            or record.get("fixture_receipt_sha256")
            != attention["tensor_fixture_receipt_sha256"]
        ):
            raise ValueError(f"composed attention memory contract mismatch: {name}")
    if set(attention["tensors"]) != set(COMPOSED_ATTENTION_OBSERVED_TENSORS):
        raise ValueError("composed attention observed-memory schema is incomplete")

    linked = attention.get("linked_mlp")
    if not isinstance(linked, dict) or (
        linked.get("schema") != mlp["schema"]
        or linked.get("receipt_sha256") != mlp["receipt_sha256"]
        or linked.get("tensor_fixture_receipt_sha256")
        != mlp["tensor_fixture_receipt_sha256"]
    ):
        raise ValueError("composed attention linked MLP fixture authority mismatch")
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        attention_record = attention["tensors"][name]
        mlp_record = mlp["tensors"][name]
        linked_record = linked.get(name)
        if not isinstance(linked_record, dict):
            raise ValueError(f"missing linked MLP tensor authority: {name}")
        for field in (
            "shape",
            "dtype",
            "bytes",
            "semantic",
            "canonical_sha256",
            "little_endian_int64_sha256",
        ):
            if (
                linked_record.get(field) != mlp_record.get(field)
                or attention_record.get(field) != mlp_record.get(field)
            ):
                raise ValueError(f"linked MLP tensor authority mismatch: {name}")
        if attention_record.get("values") != mlp_record.get("values"):
            raise ValueError(f"linked MLP tensor values mismatch: {name}")
    return attention, mlp


def _composed_attention_schema_authority(
    attention: dict[str, Any], mlp: dict[str, Any]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": COMPOSED_ATTENTION_SCHEMA,
        "attention_fixture_schema": attention["schema"],
        "mlp_fixture_schema": mlp["schema"],
        "layer": 0,
        "prompt_tokens": attention["prompt_tokens"],
        "rows": 4,
        "heads": 16,
        "head_width": 4,
        "arithmetic": attention["arithmetic"],
        "source_memories": list(COMPOSED_ATTENTION_SOURCE_MEMORIES),
        "checkpoint_memories": list(COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS),
        "observed_memories": list(COMPOSED_ATTENTION_OBSERVED_TENSORS),
        "handoffs": [
            "ln1_output_q16_16_to_qkv_input_qdq",
            "qkv_output_q16_16_to_causal_attention",
            "attention_context_q16_16_to_out_input_qdq",
            "out_output_q16_16_and_block_input_q16_16_to_attention_residual",
            "attention_residual_q16_16_to_ln2",
            "ln2_output_q16_16_to_c_fc_input_qdq",
        ],
    }
    payload["receipt_sha256"] = _canonical_sha256(payload)
    return payload


def _residual_add_phase() -> tuple[list[str], list[str], str]:
    """Add out-projection Q16.16 to the block input in hardware memory."""
    cells = [
        "attention_residual_counter = std_reg(9);",
        "attention_residual_lt = std_lt(9);",
        "attention_residual_increment = std_add(9);",
        "attention_residual_address = std_slice(9, 8);",
        "attention_residual_add = std_sadd(64);",
    ]
    wires = ["""attention_residual_address.in = attention_residual_counter.out;
    group attention_residual_init_counter {
      attention_residual_counter.in = 9'd0;
      attention_residual_counter.write_en = 1'd1;
      attention_residual_init_counter[done] = attention_residual_counter.done;
    }
    group attention_residual_read {
      block_input_q16_16.addr0 = attention_residual_address.out;
      block_input_q16_16.content_en = 1'd1;
      out_output_q16_16.addr0 = attention_residual_address.out;
      out_output_q16_16.content_en = 1'd1;
      attention_residual_read[done] = (block_input_q16_16.done & out_output_q16_16.done) ? 1'd1;
    }
    group attention_residual_write {
      attention_residual_add.left = block_input_q16_16.read_data;
      attention_residual_add.right = out_output_q16_16.read_data;
      attention_residual_q16_16.addr0 = attention_residual_address.out;
      attention_residual_q16_16.content_en = 1'd1;
      attention_residual_q16_16.write_data = attention_residual_add.out;
      attention_residual_q16_16.write_en = 1'd1;
      attention_residual_write[done] = attention_residual_q16_16.done;
    }
    group attention_residual_increment_counter {
      attention_residual_increment.left = attention_residual_counter.out;
      attention_residual_increment.right = 9'd1;
      attention_residual_counter.in = attention_residual_increment.out;
      attention_residual_counter.write_en = 1'd1;
      attention_residual_increment_counter[done] = attention_residual_counter.done;
    }
    comb group attention_residual_condition {
      attention_residual_lt.left = attention_residual_counter.out;
      attention_residual_lt.right = 9'd256;
    }"""]
    control = """      attention_residual_init_counter;
      while attention_residual_lt.out with attention_residual_condition {
        seq { attention_residual_read; attention_residual_write; attention_residual_increment_counter; }
      }"""
    return cells, wires, control


def _ln2_composed_phase() -> tuple[list[str], list[str], str]:
    """Namespace the proven exact LayerNorm datapath for the ln_2 handoff."""
    shared = _mlp_lowerer()
    cells_text, wires_text, control_text = shared._component_sections(
        _ln1_kernel_futil()
    )
    external_lines = (
        "    @external block_input_q16_16 = seq_mem_d1(64, 256, 8);\n",
        "    @external ln1_gamma_q16_16 = seq_mem_d1(64, 64, 6);\n",
        "    @external ln1_beta_q16_16 = seq_mem_d1(64, 64, 6);\n",
        "    @external ln1_q16_16 = seq_mem_d1(64, 256, 8);\n",
    )
    for line in external_lines:
        if line not in cells_text:
            raise ValueError("proven LayerNorm memory ABI changed")
        cells_text = cells_text.replace(line, "", 1)
    memory_replacements = {
        "block_input_q16_16": "attention_residual_q16_16",
        "ln1_gamma_q16_16": "ln2_gamma_q16_16",
        "ln1_beta_q16_16": "ln2_beta_q16_16",
        "ln1_q16_16": "ln2_output_q16_16",
    }
    for old, new in memory_replacements.items():
        wires_text = re.sub(rf"\b{re.escape(old)}\b", new, wires_text)

    cell_names = re.findall(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", cells_text
    )
    group_names = re.findall(
        r"(?m)^\s*(?:comb\s+)?group\s+([A-Za-z_][A-Za-z0-9_]*)",
        wires_text,
    )
    replacements = {
        name: f"ln2_{name}" for name in (*cell_names, *group_names)
    }
    for old in sorted(replacements, key=len, reverse=True):
        replacement = replacements[old]
        cells_text = re.sub(rf"\b{re.escape(old)}\b", replacement, cells_text)
        wires_text = re.sub(rf"\b{re.escape(old)}\b", replacement, wires_text)
        control_text = re.sub(
            rf"\b{re.escape(old)}\b", replacement, control_text
        )
    return (
        [line.strip() for line in cells_text.splitlines() if line.strip()],
        [wires_text.strip()],
        control_text.strip(),
    )


def _composed_attention_kernel_futil() -> str:
    """Append only the approved attention-to-MLP phases to the Task 3 main."""
    shared = _mlp_lowerer()
    causal_futil = _causal_kernel_futil()
    causal_cells, causal_wires, causal_control = shared._component_sections(
        causal_futil
    )
    additional_memories = [
        "@external out_input_codes_i8 = seq_mem_d1(8, 256, 8);",
        "@external out_input_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external out_input_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external out_weight_codes_i8 = seq_mem_d1(8, 4096, 12);",
        "@external out_weight_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external out_bias_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external out_accumulator_i64 = seq_mem_d1(64, 256, 8);",
        "@external out_post_weight_rescale_bias_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external out_output_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external out_output_codes_i8 = seq_mem_d1(8, 256, 8);",
        "@external out_output_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external attention_residual_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external ln2_gamma_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external ln2_beta_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external ln2_output_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external c_fc_input_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external c_fc_input_codes_i8 = seq_mem_d1(8, 256, 8);",
        "@external c_fc_input_q16_16 = seq_mem_d1(64, 256, 8);",
    ]
    phases = [
        shared._activation_qdq_phase(
            "out_input_qdq",
            "attention_context_q16_16",
            "out_input_scale_q8_24",
            "out_input_codes_i8",
            "out_input_q16_16",
            256,
            64,
        ),
        shared._gemv_phase(
            "out_gemv",
            "out_input_codes_i8",
            "out_input_scale_q8_24",
            "out_weight_codes_i8",
            "out_accumulator_i64",
            4,
            64,
            64,
        ),
        shared._requantize_phase(
            "out_requant",
            "out_accumulator_i64",
            "out_weight_scale_q8_24",
            "out_bias_q16_16",
            "out_output_scale_q8_24",
            "out_post_weight_rescale_bias_q16_16",
            "out_output_codes_i8",
            "out_output_q16_16",
            256,
            64,
        ),
        _residual_add_phase(),
        _ln2_composed_phase(),
        shared._activation_qdq_phase(
            "c_fc_input_qdq",
            "ln2_output_q16_16",
            "c_fc_input_scale_q8_24",
            "c_fc_input_codes_i8",
            "c_fc_input_q16_16",
            256,
            64,
        ),
    ]
    phase_cells = [cell for cells, _, _ in phases for cell in cells]
    phase_wires = [wire for _, wires, _ in phases for wire in wires]
    phase_controls = [control for _, _, control in phases]
    memory_cells = [
        line.strip() for line in causal_cells.splitlines() if line.strip()
    ]
    futil = f'''// Generated exact TinyStories-1M block-0 attention crossing.
// Task 3 semantics are retained, followed by out projection, residual, ln_2,
// and the hardware-owned c_fc input Q/DQ boundary.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
{shared._indent_lines(memory_cells + additional_memories + phase_cells, 4)}
  }}
  wires {{
{causal_wires.rstrip()}
{shared._indent_lines(phase_wires, 4)}
  }}
  control {{
    seq {{
{causal_control.rstrip()}
{shared._indent_lines(phase_controls, 0)}
    }}
  }}
}}
'''
    if len(re.findall(r"(?m)^component\s+main\b", futil)) != 1:
        raise ValueError("composed attention generator did not emit exactly one main")
    required_direct_reads = (
        "out_input_qdq_numerator_lshift.left = attention_context_q16_16.read_data;",
        "attention_residual_add.right = out_output_q16_16.read_data;",
        "ln2_sum_add.right = attention_residual_q16_16.read_data;",
        "c_fc_input_qdq_numerator_lshift.left = ln2_output_q16_16.read_data;",
    )
    for statement in required_direct_reads:
        if statement not in futil:
            raise ValueError(f"missing direct generated-memory handoff: {statement}")
    for name in COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS:
        if f"{name}.write_en = 1'd1" not in futil:
            raise ValueError(f"composed checkpoint is not hardware-written: {name}")
    return futil


def generate_composed_attention_kernel(
    fixture_path: Path, mlp_fixture_path: Path
) -> CalyxArtifact:
    attention, mlp = _checked_composed_attention_fixtures(
        fixture_path, mlp_fixture_path
    )
    futil = _composed_attention_kernel_futil()
    shared_path = ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": COMPOSED_ATTENTION_SCHEMA,
            "fixture_receipt_sha256": attention["receipt_sha256"],
            "tensor_fixture_receipt_sha256": attention[
                "tensor_fixture_receipt_sha256"
            ],
            "linked_mlp_receipt_sha256": mlp["receipt_sha256"],
            "linked_mlp_tensor_fixture_receipt_sha256": mlp[
                "tensor_fixture_receipt_sha256"
            ],
            "fixture_authority": _fixture_authority(attention),
            "linked_mlp_fixture_authority": _mlp_lowerer()._fixture_authority(mlp),
            "schema_authority": _composed_attention_schema_authority(attention, mlp),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "shared_phase_generator_sha256": hashlib.sha256(
                shared_path.read_bytes()
            ).hexdigest(),
            "component_count": 1,
            "host_preload_memories": list(COMPOSED_ATTENTION_SOURCE_MEMORIES),
            "hardware_owned_memories": list(
                COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS
            ),
            "observed_memories": list(COMPOSED_ATTENTION_OBSERVED_TENSORS),
            "calyx_disabled_passes": ["cell-share"],
            "host_intermediate": False,
        },
    )


def _generated_composed_attention_harness(attention: dict[str, Any]) -> str:
    """Preload sources, zero computed memories, and print all post-run words."""
    declarations: list[str] = []
    preload_groups: list[str] = []
    cpp_names: list[str] = []
    for index, name in enumerate(COMPOSED_ATTENTION_SOURCE_MEMORIES):
        values = _flatten_tensor(attention, name)
        cpp_name = f"kSource{index:02d}"
        cpp_names.append(cpp_name)
        declarations.append(
            f"// {name}\nstatic const std::int64_t {cpp_name}[{len(values)}] = "
            f"{{{_cpp_values(values)}}};"
        )
        cast = "std::uint8_t" if name.endswith("codes_i8") else "std::uint64_t"
        preload_groups.append(
            f"  for (unsigned i = 0; i < {len(values)}; ++i)\n"
            f"    root->main__DOT__{name}__DOT__mem[i] = "
            f"static_cast<{cast}>({cpp_name}[i]);"
        )

    zero_groups = [
        f"  for (unsigned i = 0; i < "
        f"{attention['tensors'][name]['bytes'] // 8}; ++i)\n"
        f"    root->main__DOT__{name}__DOT__mem[i] = 0;"
        for name in COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS
    ]
    print_groups = [
        _cpp_print_memory(
            name,
            attention["tensors"][name]["bytes"] // 8,
            signed_i8=name.endswith("codes_i8"),
        )
        for name in COMPOSED_ATTENTION_OBSERVED_TENSORS
    ]
    harness = f'''// Generated source-only attention crossing harness.
// All non-source memories are hardware-owned and begin at a non-oracle zero.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

{chr(10).join(declarations)}

static void tick(Vmain& model) {{
  model.clk = 0;
  model.eval();
  model.clk = 1;
  model.eval();
}}

int main(int argc, char** argv) {{
  Verilated::commandArgs(argc, argv);
  Vmain model;
  auto* root = model.rootp;
{chr(10).join(preload_groups)}
{chr(10).join(zero_groups)}
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 10000000) {{
    tick(model);
    ++cycles;
  }}
  const bool complete = model.done && cycles > 256;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "timeout")
            << "\\\",\\\"cycles\\\":" << cycles;
{chr(10).join(print_groups)}
  std::cout << "}}\\n";
  return complete ? 0 : 1;
}}
'''
    found_arrays = re.findall(
        r"^static const std::int64_t\s+(kSource[0-9]+)\[",
        harness,
        re.MULTILINE,
    )
    if found_arrays != cpp_names:
        raise ValueError("composed attention source preload whitelist mismatch")
    if "kExpected" in harness:
        raise ValueError("composed attention harness contains an output oracle")
    for name in COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS:
        assignment = rf"main__DOT__{re.escape(name)}__DOT__mem\[i\]\s*=\s*([^;]+);"
        if re.findall(assignment, harness) != ["0"]:
            raise ValueError(f"host writes composed attention checkpoint: {name}")
    return harness


def _artifact_record(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _require_composed_checkpoint(
    name: str, observed: list[int], attention: dict[str, Any]
) -> dict[str, object]:
    expected = _flatten_tensor(attention, name)
    if len(observed) != len(expected):
        raise RuntimeError(
            f"generated-SV composed {name} length mismatch: "
            f"{len(observed)} != {len(expected)}"
        )
    for index, (actual, wanted) in enumerate(zip(observed, expected, strict=True)):
        if actual != wanted:
            raise RuntimeError(
                f"generated-SV composed {name} mismatch at checkpoint {index}: "
                f"observed {actual}, expected {wanted}"
            )
    digest = _little_endian_i64_sha256(observed)
    if digest != attention["tensors"][name]["little_endian_int64_sha256"]:
        raise RuntimeError(f"generated-SV composed {name} hash mismatch")
    return {"count": len(observed), "little_endian_int64_sha256": digest}


def _validate_yosys_stat(stdout: str) -> dict[str, int]:
    if "=== main ===" not in stdout or "=== design hierarchy ===" not in stdout:
        raise RuntimeError("Yosys stat did not report the generated main hierarchy")
    hierarchy = stdout.rsplit("=== design hierarchy ===", maxsplit=1)[1]
    values: dict[str, int] = {}
    for key, label in (
        ("cells", "cells"),
        ("memories", "memories"),
        ("memory_bits", "memory bits"),
    ):
        match = re.search(rf"(?m)^\s*(\d+)\s+{re.escape(label)}\s*$", hierarchy)
        if match is None:
            raise RuntimeError(f"Yosys stat is missing hierarchy {label}")
        values[key] = int(match.group(1))
    if any(value <= 0 for value in values.values()):
        raise RuntimeError("Yosys stat reported an empty generated hierarchy")
    return values


def run_composed_attention_sv(
    fixture_path: Path, mlp_fixture_path: Path
) -> dict[str, object]:
    """Compile and exhaustively observe the one-main attention crossing."""
    artifact = generate_composed_attention_kernel(fixture_path, mlp_fixture_path)
    attention, mlp = _checked_composed_attention_fixtures(
        fixture_path, mlp_fixture_path
    )
    shared_path = ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    if (
        artifact.provenance.get("fixture_authority")
        != _fixture_authority(attention)
        or artifact.provenance.get("linked_mlp_fixture_authority")
        != _mlp_lowerer()._fixture_authority(mlp)
        or artifact.provenance.get("schema_authority")
        != _composed_attention_schema_authority(attention, mlp)
        or artifact.provenance.get("shared_phase_generator_sha256")
        != hashlib.sha256(shared_path.read_bytes()).hexdigest()
    ):
        raise ValueError("composed attention fixture/schema authority mismatch")
    futil_sha256 = hashlib.sha256(artifact.futil.encode("utf-8")).hexdigest()
    if artifact.provenance.get("futil_sha256") != futil_sha256:
        raise ValueError("composed attention Futil authority mismatch")

    artifact_dir = COMPOSED_ATTENTION_ARTIFACT_DIRECTORY
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "attention-crossing.futil"
    sv_path = artifact_dir / "main.sv"
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness = _generated_composed_attention_harness(attention)
    harness_path.write_text(harness, encoding="utf-8")

    calyx = _calyx_install()
    common = [
        str(calyx / "bin/calyx"),
        str(futil_path),
        "-l",
        str(calyx / "share/calyx"),
        "-d",
        "cell-share",
    ]
    simulator_command = [*common, "-b", "verilog", "-o", str(sv_path)]
    simulator_compile = subprocess.run(
        simulator_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if simulator_compile.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(
            "composed attention Calyx-to-SV failed: "
            f"{simulator_compile.stderr.strip()}"
        )
    synthesis_command = [
        *common,
        "--synthesis",
        "--disable-verify",
        "-b",
        "verilog",
        "-o",
        str(synthesis_sv_path),
    ]
    synthesis_compile = subprocess.run(
        synthesis_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if synthesis_compile.returncode != 0 or not synthesis_sv_path.is_file():
        raise RuntimeError(
            "composed attention Calyx synthesis-to-SV failed: "
            f"{synthesis_compile.stderr.strip()}"
        )

    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_attention_crossing_harness"
    verilator_command = [
        "verilator",
        "--cc",
        "--exe",
        "--build",
        "--top-module",
        "main",
        "--public-flat-rw",
        "--Mdir",
        str(verilator_dir),
        "-CFLAGS",
        "-std=c++17",
        "-o",
        executable.name,
        str(sv_path),
        str(harness_path),
    ]
    verilator_compile = subprocess.run(
        verilator_command,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    if verilator_compile.returncode != 0 or not executable.is_file():
        raise RuntimeError(
            f"composed attention Verilator build failed: {verilator_compile.stderr.strip()}"
        )
    simulated = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if simulated.returncode != 0:
        raise RuntimeError(
            "generated-SV composed attention harness failed: "
            f"{simulated.stdout.strip()} {simulated.stderr.strip()}"
        )
    try:
        observed = json.loads(
            next(
                line
                for line in reversed(simulated.stdout.splitlines())
                if line.startswith("{")
            )
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError(
            "generated-SV composed attention harness did not emit JSON: "
            f"{simulated.stdout!r}"
        ) from error
    if observed.get("status") != "ok" or not isinstance(observed.get("cycles"), int):
        raise RuntimeError(
            f"generated-SV composed attention observation is invalid: {observed}"
        )

    checkpoint_receipts: dict[str, object] = {}
    for name in COMPOSED_ATTENTION_OBSERVED_TENSORS:
        values = observed.get(name)
        if not isinstance(values, list) or not all(
            isinstance(value, int) for value in values
        ):
            raise RuntimeError(f"generated-SV composed checkpoint invalid: {name}")
        checkpoint_receipts[name] = _require_composed_checkpoint(
            name, values, attention
        )
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        digest = checkpoint_receipts[name]["little_endian_int64_sha256"]
        if digest != mlp["tensors"][name]["little_endian_int64_sha256"]:
            raise RuntimeError(f"generated-SV linked MLP checkpoint mismatch: {name}")

    generated_artifacts = {
        "futil": _artifact_record(futil_path),
        "sv": _artifact_record(sv_path),
        "synthesis_sv": _artifact_record(synthesis_sv_path),
        "harness": _artifact_record(harness_path),
    }
    if generated_artifacts["futil"]["sha256"] != futil_sha256:
        raise RuntimeError("written composed attention Futil hash mismatch")
    yosys_command = [
        "yosys",
        "-p",
        f"read_verilog -sv {synthesis_sv_path}; hierarchy -check -top main; stat",
    ]
    yosys = subprocess.run(
        yosys_command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if yosys.returncode != 0:
        raise RuntimeError(f"composed attention Yosys stat failed: {yosys.stderr.strip()}")
    synthesis_resources = _validate_yosys_stat(yosys.stdout)

    observed_receipt: dict[str, object] = {
        "checkpoints": checkpoint_receipts,
        "ln2_q16_16_sha256": checkpoint_receipts["ln2_output_q16_16"][
            "little_endian_int64_sha256"
        ],
    }
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        observed_receipt[f"{name}_sha256"] = checkpoint_receipts[name][
            "little_endian_int64_sha256"
        ]
    receipt: dict[str, object] = {
        "schema": COMPOSED_ATTENTION_SCHEMA,
        "fixture_receipt_sha256": attention["receipt_sha256"],
        "tensor_fixture_receipt_sha256": attention[
            "tensor_fixture_receipt_sha256"
        ],
        "linked_mlp_receipt_sha256": mlp["receipt_sha256"],
        "linked_mlp_tensor_fixture_receipt_sha256": mlp[
            "tensor_fixture_receipt_sha256"
        ],
        "fixture_authority": _fixture_authority(attention),
        "linked_mlp_fixture_authority": _mlp_lowerer()._fixture_authority(mlp),
        "schema_authority": _composed_attention_schema_authority(attention, mlp),
        "execution": {
            "component_count": 1,
            "simulator_runs": 1,
            "host_intermediate": False,
            "cycles": observed["cycles"],
            "host_preload_memories": list(COMPOSED_ATTENTION_SOURCE_MEMORIES),
            "hardware_owned_memories": list(
                COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS
            ),
            "traversal": "task3_causal_then_out_projection_then_residual_then_ln2_then_c_fc_input_qdq",
            "ln2_to_c_fc_handoff": "ln2_output_q16_16_hardware_memory",
        },
        "observed": observed_receipt,
        "generated_artifacts": generated_artifacts,
        "calyx_compile_policy": {
            "disabled_passes": ["cell-share"],
            "reason": "preserve explicitly staged fixed-point arithmetic latches",
            "simulator_command": simulator_command,
            "synthesis_command": synthesis_command,
            "same_futil_sha256": generated_artifacts["futil"]["sha256"],
        },
        "synthesis": {
            "status": "passed",
            "same_futil_sha256": generated_artifacts["futil"]["sha256"],
            "synthesis_sv_sha256": generated_artifacts["synthesis_sv"]["sha256"],
            "command": yosys_command,
            "resources": synthesis_resources,
        },
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    if receipt["receipt_sha256"] != _canonical_sha256(unsigned):
        raise RuntimeError("composed attention receipt self-hash mismatch")
    return receipt
