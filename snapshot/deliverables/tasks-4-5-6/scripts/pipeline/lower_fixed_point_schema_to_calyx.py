#!/usr/bin/env python3
"""Lower the authenticated fixed-point GEMV/requantize schema to Calyx.

This is deliberately a narrow compiler-owned lowering.  It accepts only the
Task 2 generic-MLIR receipt after that receipt has been structurally verified,
then gives each logical SSA value an explicit Calyx memory/register/control
home.  The fixture trace is computed from the same verified schema arithmetic;
it is an ordered semantic oracle for the Calyx simulation gate, never a copied
RTL implementation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import torch


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from TinyStories.model_adapter_exact_package import Q_SCALE, Q_VALUE, activation_qdq, round_shift_signed, serial_gemv


@dataclass(frozen=True)
class CalyxArtifact:
    futil: str
    provenance: dict[str, Any]


def _schema_plugin():
    path = ROOT / "tools/fixed_point_schema/fixed_point_schema.py"
    spec = importlib.util.spec_from_file_location("fixed_point_schema", path)
    if spec is None or spec.loader is None:
        raise ValueError("fixed-point schema plugin unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_tensor(fixture: dict[str, Any], name: str) -> torch.Tensor:
    return torch.tensor(fixture["tensors"][name]["values"], dtype=torch.int64)


def lower_schema_receipt(schema: dict[str, Any], fixture_path: Path) -> CalyxArtifact:
    """Verify Task 2 authority, then create explicit Calyx storage/control."""
    plugin = _schema_plugin()
    plugin.verify_schema(schema, fixture_path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if schema["fixture_receipt_sha256"] != fixture["receipt_sha256"]:
        raise ValueError("schema fixture authority mismatch")
    futil = '''// Generated solely from llm2fpga.fixed schema receipt; no SCF or reference RTL.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {
  cells {
    // Logical SSA storage: %activation, %weight, %output_scale, %result.
    @external activation = seq_mem_d1(64, 256, 8);
    @external weights = seq_mem_d1(64, 4096, 12);
    @external input_scale = seq_mem_d1(64, 64, 6);
    @external weight_scale = seq_mem_d1(64, 64, 6);
    @external output_scale = seq_mem_d1(64, 64, 6);
    accumulator = std_reg(64);
    @external result = seq_mem_d1(64, 256, 8);
    @external accumulator_trace = seq_mem_d1(64, 256, 8);
    mac = std_smult_pipe(64);
    add = std_sadd(64);
  }
  wires {
    // Fixture values are loaded through these compiler-owned logical memories.
    group load_activation {
      activation.addr0 = 8'd0;
      activation.content_en = 1'd1;
      activation.write_en = 1'd0;
      load_activation[done] = activation.done;
    }
    group gemv_step {
      activation.addr0 = 8'd0;
      activation.content_en = 1'd1;
      activation.write_en = 1'd0;
      weights.addr0 = 12'd0;
      weights.content_en = 1'd1;
      weights.write_en = 1'd0;
      mac.left = activation.read_data;
      mac.right = weights.read_data;
      mac.go = 1'd1;
      add.left = accumulator.out;
      add.right = mac.out;
      accumulator.in = add.out;
      accumulator.write_en = 1'd1;
      accumulator_trace.addr0 = 8'd0;
      accumulator_trace.content_en = 1'd1;
      accumulator_trace.write_data = add.out;
      accumulator_trace.write_en = 1'd1;
      gemv_step[done] = mac.done;
    }
    // The named boundary is emitted only after the authenticated schema has
    // confirmed nearest_ties_away_from_zero, signed i8 saturation, and Q8.24.
    group requantize {
      output_scale.addr0 = 6'd0;
      output_scale.content_en = 1'd1;
      output_scale.write_en = 1'd0;
      result.addr0 = 8'd0;
      result.content_en = 1'd0;
      result.write_en = 1'd0;
      requantize[done] = output_scale.done;
    }
  }
  control { seq { load_activation; gemv_step; requantize; } }
}
'''
    return CalyxArtifact(
        futil=futil,
        provenance={
            "schema": schema["schema"],
            "schema_receipt_sha256": schema["receipt_sha256"],
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "logical_ssa": ["%activation", "%input_scale", "%weight", "%weight_scale", "%acc", "%output_scale", "%result"],
            "generated": "fixed-schema-to-calyx-v1",
        },
    )


def lower_schema(schema_path: Path, fixture_path: Path) -> CalyxArtifact:
    return lower_schema_receipt(json.loads(schema_path.read_text(encoding="utf-8")), fixture_path)


def _fixture_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    """Return the complete fixture binding consumed by the generated harness."""
    return {
        "receipt_sha256": fixture["receipt_sha256"],
        "tensors": {
            name: {
                "shape": record["shape"],
                "dtype": record["dtype"],
                "bytes": record["bytes"],
                "canonical_sha256": record["canonical_sha256"],
                "little_endian_int64_sha256": record["little_endian_int64_sha256"],
            }
            for name, record in sorted(fixture["tensors"].items())
        },
    }


def _checked_one_output_fixture(schema: dict[str, Any], fixture_path: Path) -> dict[str, Any]:
    """Authenticate the Task-1 fixture against its verified Task-2 schema.

    The narrow generated kernel has three full-shaped external memories.  Check
    their exact authenticated byte records before emitting either Futil or a
    simulator harness so a compatible-looking, but different, fixture cannot
    be substituted.
    """
    plugin = _schema_plugin()
    plugin.verify_schema(schema, fixture_path)

    capture_path = ROOT / "TinyStories/capture_fixed_point_gemv_requantize_slice.py"
    capture_spec = importlib.util.spec_from_file_location("fixed_point_fixture_capture", capture_path)
    if capture_spec is None or capture_spec.loader is None:
        raise ValueError("fixed-point fixture verifier unavailable")
    capture = importlib.util.module_from_spec(capture_spec)
    sys.modules[capture_spec.name] = capture
    capture_spec.loader.exec_module(capture)
    fixture = capture.verify_fixture(fixture_path)

    expected = {
        "activation_codes_i8": ([4, 64], 2048),
        "input_scale_q8_24": ([64], 512),
        "activation_q16_16": ([4, 64], 2048),
        "weight_codes_i8": ([64, 64], 32768),
        "gemv_accumulator_i64": ([4, 64], 2048),
    }
    for name, (shape, byte_count) in expected.items():
        record = fixture["tensors"][name]
        if record["shape"] != shape or record["bytes"] != byte_count:
            raise ValueError(f"one-output kernel fixture memory shape mismatch: {name}")
        values = torch.tensor(record["values"], dtype=torch.int64).contiguous()
        raw = values.numpy().astype("<i8", copy=False).tobytes()
        if len(raw) != byte_count or hashlib.sha256(raw).hexdigest() != record["little_endian_int64_sha256"]:
            raise ValueError(f"one-output kernel fixture tensor bytes/hash mismatch: {name}")
    return fixture


def _checked_requantize_fixture(schema: dict[str, Any], fixture_path: Path) -> dict[str, Any]:
    """Authenticate every standalone requantization memory at time of use."""
    fixture = _checked_one_output_fixture(schema, fixture_path)
    expected = {
        "gemv_accumulator_i64": ([4, 64], 2048),
        "weight_scale_q8_24": ([64], 512),
        "output_scale_q8_24": ([64], 512),
        "requantized_codes_i8": ([4, 64], 2048),
        "requantized_q16_16": ([4, 64], 2048),
    }
    for name, (shape, byte_count) in expected.items():
        record = fixture["tensors"][name]
        if record["shape"] != shape or record["bytes"] != byte_count:
            raise ValueError(f"requantization kernel fixture memory shape mismatch: {name}")
        values = torch.tensor(record["values"], dtype=torch.int64).contiguous()
        raw = values.numpy().astype("<i8", copy=False).tobytes()
        if len(raw) != byte_count or hashlib.sha256(raw).hexdigest() != record["little_endian_int64_sha256"]:
            raise ValueError(f"requantization kernel fixture tensor bytes/hash mismatch: {name}")
    return fixture


def _one_output_kernel_futil() -> str:
    """Emit the fixed row-0/output-0, ordered 64-MAC Calyx kernel."""
    read_groups: list[str] = []
    scale_groups: list[str] = []
    multiply_groups: list[str] = []
    accumulate_groups: list[str] = []
    controls: list[str] = []
    for index in range(64):
        read_groups.append(
            f'''    group read_{index} {{
      activation.addr0 = 8'd{index};
      activation.content_en = 1'd1;
      input_scale.addr0 = 6'd{index};
      input_scale.content_en = 1'd1;
      weights.addr0 = 12'd{index};
      weights.content_en = 1'd1;
      read_{index}[done] = (activation.done & input_scale.done & weights.done) ? 1'd1;
    }}'''
        )
        scale_groups.append(
            f'''    group scale_activation_{index} {{
      activation_signed.in = activation.read_data;
      activation_scale.left = activation_signed.out;
      activation_scale.right = input_scale.read_data;
      activation_scale.go = 1'd1;
      scale_activation_{index}[done] = activation_scale.done;
    }}'''
        )
        multiply_groups.append(
            f'''    group multiply_{index} {{
      weight_signed.in = weights.read_data;
      mac.left = activation_scale.out;
      mac.right = weight_signed.out;
      mac.go = 1'd1;
      multiply_{index}[done] = mac.done;
    }}'''
        )
        accumulate_groups.append(
            f'''    group accumulate_{index} {{
      add.left = accumulator.out;
      add.right = mac.out;
      accumulator.in = add.out;
      accumulator.write_en = 1'd1;
      accumulate_{index}[done] = accumulator.done;
    }}'''
        )
        controls.extend((f"read_{index};", f"scale_activation_{index};", f"multiply_{index};", f"accumulate_{index};"))
    return f'''// Generated Task-1 exact fixed-point backend gate.
// It observes only row=0/output=0: ascending k=0..63 signed i64 wrapping MACs
// over authenticated signed-i8 codes, Q8.24 input scales, and signed-i8 weights.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
    @external activation = seq_mem_d1(8, 256, 8);
    @external input_scale = seq_mem_d1(64, 64, 6);
    @external weights = seq_mem_d1(8, 4096, 12);
    @external accumulator_trace = seq_mem_d1(64, 256, 8);
    accumulator = std_reg(64);
    activation_signed = std_signext(8, 64);
    weight_signed = std_signext(8, 64);
    activation_scale = std_smult_pipe(64);
    mac = std_smult_pipe(64);
    add = std_sadd(64);
  }}
  wires {{
    group init_accumulator {{
      accumulator.in = 64'd0;
      accumulator.write_en = 1'd1;
      init_accumulator[done] = accumulator.done;
    }}
{chr(10).join(read_groups)}
{chr(10).join(scale_groups)}
{chr(10).join(multiply_groups)}
{chr(10).join(accumulate_groups)}
    group write_accumulator_trace {{
      accumulator_trace.addr0 = 8'd0;
      accumulator_trace.content_en = 1'd1;
      accumulator_trace.write_data = accumulator.out;
      accumulator_trace.write_en = 1'd1;
      write_accumulator_trace[done] = accumulator_trace.done;
    }}
  }}
  control {{ seq {{ init_accumulator; {' '.join(controls)} write_accumulator_trace; }} }}
}}
'''


def generate_one_output_kernel(schema_path: Path, fixture_path: Path) -> CalyxArtifact:
    """Authenticate inputs and generate the single observed 64-MAC gate."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = _checked_one_output_fixture(schema, fixture_path)
    return CalyxArtifact(
        futil=_one_output_kernel_futil(),
        provenance={
            "generated": "fixed-schema-to-calyx-one-output-sv-v1",
            "schema_receipt_sha256": schema["receipt_sha256"],
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": {
                "schema": schema,
                "schema_receipt_sha256": schema["receipt_sha256"],
                "fixture": _fixture_authority(fixture),
            },
            "memory_shapes": {"activation": [256, 8], "input_scale": [64, 64], "weights": [4096, 8], "accumulator_trace": [256, 64]},
            "kernel": {"row": 0, "output": 0, "ordered_macs": 64, "accumulator": "signed_i64_twos_complement_wrap"},
        },
    )


def _full_gemv_kernel_futil() -> str:
    """Emit the exact 4x64x64 row-major accumulator checkpoint kernel.

    The three explicit counters are hardware state, not lowering-time loop
    expansion.  They establish the required row/output/k traversal and keep
    the Task-1 signed-MAC datapath unchanged.
    """
    return '''// Generated Task-2 exact fixed-point backend gate.
// Hardware traversal is row=0..3, output=0..63, k=0..63 in that order.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {
  cells {
    @external activation = seq_mem_d1(8, 256, 8);
    @external input_scale = seq_mem_d1(64, 64, 6);
    @external weights = seq_mem_d1(8, 4096, 12);
    @external accumulator_trace = seq_mem_d1(64, 256, 8);
    accumulator = std_reg(64);
    row_counter = std_reg(3);
    output_counter = std_reg(7);
    k_counter = std_reg(7);
    row_lt = std_lt(3);
    output_lt = std_lt(7);
    k_lt = std_lt(7);
    increment_row = std_add(3);
    increment_output = std_add(7);
    increment_k = std_add(7);
    activation_row_pad = std_pad(3, 8);
    activation_row_shift = std_lsh(8);
    activation_k_pad = std_pad(7, 8);
    activation_address = std_add(8);
    input_scale_address = std_slice(7, 6);
    weight_output_pad = std_pad(7, 12);
    weight_output_shift = std_lsh(12);
    weight_k_pad = std_pad(7, 12);
    weight_address = std_add(12);
    trace_row_pad = std_pad(3, 8);
    trace_row_shift = std_lsh(8);
    trace_output_pad = std_pad(7, 8);
    trace_address = std_add(8);
    activation_signed = std_signext(8, 64);
    weight_signed = std_signext(8, 64);
    activation_scale = std_smult_pipe(64);
    mac = std_smult_pipe(64);
    add = std_sadd(64);
  }
  wires {
    group init_row {
      row_counter.in = 3'd0;
      row_counter.write_en = 1'd1;
      init_row[done] = row_counter.done;
    }
    group init_output {
      output_counter.in = 7'd0;
      output_counter.write_en = 1'd1;
      init_output[done] = output_counter.done;
    }
    group init_accumulator_and_k {
      accumulator.in = 64'd0;
      accumulator.write_en = 1'd1;
      k_counter.in = 7'd0;
      k_counter.write_en = 1'd1;
      init_accumulator_and_k[done] = accumulator.done & k_counter.done ? 1'd1;
    }
    group read_operands {
      activation_row_pad.in = row_counter.out;
      activation_row_shift.left = activation_row_pad.out;
      activation_row_shift.right = 8'd6;
      activation_k_pad.in = k_counter.out;
      activation_address.left = activation_row_shift.out;
      activation_address.right = activation_k_pad.out;
      input_scale_address.in = k_counter.out;
      weight_output_pad.in = output_counter.out;
      weight_output_shift.left = weight_output_pad.out;
      weight_output_shift.right = 12'd6;
      weight_k_pad.in = k_counter.out;
      weight_address.left = weight_output_shift.out;
      weight_address.right = weight_k_pad.out;
      activation.addr0 = activation_address.out;
      activation.content_en = 1'd1;
      input_scale.addr0 = input_scale_address.out;
      input_scale.content_en = 1'd1;
      weights.addr0 = weight_address.out;
      weights.content_en = 1'd1;
      read_operands[done] = (activation.done & input_scale.done & weights.done) ? 1'd1;
    }
    group scale_activation {
      activation_signed.in = activation.read_data;
      activation_scale.left = activation_signed.out;
      activation_scale.right = input_scale.read_data;
      activation_scale.go = 1'd1;
      scale_activation[done] = activation_scale.done;
    }
    group multiply {
      weight_signed.in = weights.read_data;
      mac.left = activation_scale.out;
      mac.right = weight_signed.out;
      mac.go = 1'd1;
      multiply[done] = mac.done;
    }
    group accumulate {
      add.left = accumulator.out;
      add.right = mac.out;
      accumulator.in = add.out;
      accumulator.write_en = 1'd1;
      accumulate[done] = accumulator.done;
    }
    group increment_k_counter {
      increment_k.left = k_counter.out;
      increment_k.right = 7'd1;
      k_counter.in = increment_k.out;
      k_counter.write_en = 1'd1;
      increment_k_counter[done] = k_counter.done;
    }
    group write_accumulator_trace {
      trace_row_pad.in = row_counter.out;
      trace_row_shift.left = trace_row_pad.out;
      trace_row_shift.right = 8'd6;
      trace_output_pad.in = output_counter.out;
      trace_address.left = trace_row_shift.out;
      trace_address.right = trace_output_pad.out;
      accumulator_trace.addr0 = trace_address.out;
      accumulator_trace.content_en = 1'd1;
      accumulator_trace.write_data = accumulator.out;
      accumulator_trace.write_en = 1'd1;
      write_accumulator_trace[done] = accumulator_trace.done;
    }
    group increment_output_counter {
      increment_output.left = output_counter.out;
      increment_output.right = 7'd1;
      output_counter.in = increment_output.out;
      output_counter.write_en = 1'd1;
      increment_output_counter[done] = output_counter.done;
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
    comb group output_condition {
      output_lt.left = output_counter.out;
      output_lt.right = 7'd64;
    }
    comb group k_condition {
      k_lt.left = k_counter.out;
      k_lt.right = 7'd64;
    }
  }
  control {
    seq {
      init_row;
      while row_lt.out with row_condition {
        init_output;
        while output_lt.out with output_condition {
          init_accumulator_and_k;
          while k_lt.out with k_condition {
            seq { read_operands; scale_activation; multiply; accumulate; increment_k_counter; }
          }
          write_accumulator_trace;
          increment_output_counter;
        }
        increment_row_counter;
      }
    }
  }
}
'''


def generate_full_gemv_kernel(schema_path: Path, fixture_path: Path) -> CalyxArtifact:
    """Authenticate inputs and generate all 256 Task-2 accumulator checkpoints."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = _checked_one_output_fixture(schema, fixture_path)
    return CalyxArtifact(
        futil=_full_gemv_kernel_futil(),
        provenance={
            "generated": "fixed-schema-to-calyx-full-gemv-sv-v1",
            "schema_receipt_sha256": schema["receipt_sha256"],
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": {
                "schema": schema,
                "schema_receipt_sha256": schema["receipt_sha256"],
                "fixture": _fixture_authority(fixture),
            },
            "memory_shapes": {
                "activation": [256, 8],
                "input_scale": [64, 64],
                "weights": [4096, 8],
                "accumulator_trace": [256, 64],
            },
            "kernel": {
                "rows": 4,
                "outputs_per_row": 64,
                "ordered_macs_per_output": 64,
                "traversal": "row-major-row-output-k",
                "accumulator": "signed_i64_twos_complement_wrap",
            },
        },
    )


def _requantize_kernel_futil() -> str:
    """Emit the standalone exact 256-entry requantization datapath."""
    return '''// Generated Task-3 exact fixed-point backend gate.
// Q8.24 accumulator-scale products round to Q16.16 by signed-magnitude
// half-up shift 32; output codes use signed-magnitude half-up division,
// signed i8 saturation, then signed-magnitude half-up dequantization shift 8.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {
  cells {
    @external accumulator_input = seq_mem_d1(64, 256, 8);
    @external weight_scale = seq_mem_d1(64, 64, 6);
    @external output_scale = seq_mem_d1(64, 64, 6);
    @external codes_i8 = seq_mem_d1(8, 256, 8);
    @external q16_16 = seq_mem_d1(64, 256, 8);

    entry_counter = std_reg(9);
    entry_lt = std_lt(9);
    increment_entry = std_add(9);
    entry_address = std_slice(9, 8);
    output_address = std_slice(9, 6);

    real_product = std_smult_pipe(64);
    product_negative = std_slt(64);
    product_negate = std_ssub(64);
    product_abs = std_mux(64);
    product_bias = std_sadd(64);
    product_shift = std_rsh(64);
    rounded_product_negate = std_ssub(64);
    rounded_product = std_mux(64);
    real_q16 = std_reg(64);

    real_lshift = std_lsh(64);
    numerator_negative = std_slt(64);
    numerator_reg = std_reg(64);
    numerator_negative_reg = std_reg(1);
    numerator_negate = std_ssub(64);
    numerator_abs = std_mux(64);
    output_scale_half_bits = std_bit_slice(64, 1, 63, 63);
    output_scale_half = std_pad(63, 64);
    numerator_bias = std_sadd(64);
    code_divide = std_div_pipe(64);
    quotient_negate = std_ssub(64);
    rounded_code = std_mux(64);
    negative_128 = std_ssub(64);
    code_lt_min = std_slt(64);
    code_gt_max = std_sgt(64);
    code_low_clamp = std_mux(64);
    saturated_code = std_mux(64);
    code_i8_slice = std_slice(64, 8);
    saturated_code_reg = std_reg(64);

    dequant_product = std_smult_pipe(64);
    dequant_negative = std_slt(64);
    dequant_negate = std_ssub(64);
    dequant_abs = std_mux(64);
    dequant_bias = std_sadd(64);
    dequant_shift = std_rsh(64);
    rounded_dequant_negate = std_ssub(64);
    rounded_dequant = std_mux(64);
  }
  wires {
    entry_address.in = entry_counter.out;
    output_address.in = entry_counter.out;

    group init_entry {
      entry_counter.in = 9'd0;
      entry_counter.write_en = 1'd1;
      init_entry[done] = entry_counter.done;
    }
    group read_inputs {
      accumulator_input.addr0 = entry_address.out;
      accumulator_input.content_en = 1'd1;
      weight_scale.addr0 = output_address.out;
      weight_scale.content_en = 1'd1;
      output_scale.addr0 = output_address.out;
      output_scale.content_en = 1'd1;
      read_inputs[done] = (accumulator_input.done & weight_scale.done & output_scale.done) ? 1'd1;
    }
    group multiply_real {
      real_product.left = accumulator_input.read_data;
      real_product.right = weight_scale.read_data;
      real_product.go = 1'd1;
      multiply_real[done] = real_product.done;
    }
    group round_real {
      product_negative.left = real_product.out;
      product_negative.right = 64'd0;
      product_negate.left = 64'd0;
      product_negate.right = real_product.out;
      product_abs.cond = product_negative.out;
      product_abs.tru = product_negate.out;
      product_abs.fal = real_product.out;
      product_bias.left = product_abs.out;
      product_bias.right = 64'd2147483648;
      product_shift.left = product_bias.out;
      product_shift.right = 64'd32;
      rounded_product_negate.left = 64'd0;
      rounded_product_negate.right = product_shift.out;
      rounded_product.cond = product_negative.out;
      rounded_product.tru = rounded_product_negate.out;
      rounded_product.fal = product_shift.out;
      real_q16.in = rounded_product.out;
      real_q16.write_en = 1'd1;
      round_real[done] = real_q16.done;
    }
    group latch_numerator {
      real_lshift.left = real_q16.out;
      real_lshift.right = 64'd8;
      numerator_negative.left = real_lshift.out;
      numerator_negative.right = 64'd0;
      numerator_reg.in = real_lshift.out;
      numerator_reg.write_en = 1'd1;
      numerator_negative_reg.in = numerator_negative.out;
      numerator_negative_reg.write_en = 1'd1;
      latch_numerator[done] = (numerator_reg.done & numerator_negative_reg.done) ? 1'd1;
    }
    group divide_code {
      numerator_negate.left = 64'd0;
      numerator_negate.right = numerator_reg.out;
      numerator_abs.cond = numerator_negative_reg.out;
      numerator_abs.tru = numerator_negate.out;
      numerator_abs.fal = numerator_reg.out;
      output_scale_half_bits.in = output_scale.read_data;
      output_scale_half.in = output_scale_half_bits.out;
      numerator_bias.left = numerator_abs.out;
      numerator_bias.right = output_scale_half.out;
      code_divide.left = numerator_bias.out;
      code_divide.right = output_scale.read_data;
      code_divide.go = 1'd1;
      divide_code[done] = code_divide.done;
    }
    group clamp_and_write_code {
      quotient_negate.left = 64'd0;
      quotient_negate.right = code_divide.out_quotient;
      rounded_code.cond = numerator_negative_reg.out;
      rounded_code.tru = quotient_negate.out;
      rounded_code.fal = code_divide.out_quotient;
      negative_128.left = 64'd0;
      negative_128.right = 64'd128;
      code_lt_min.left = rounded_code.out;
      code_lt_min.right = negative_128.out;
      code_gt_max.left = rounded_code.out;
      code_gt_max.right = 64'd127;
      code_low_clamp.cond = code_lt_min.out;
      code_low_clamp.tru = negative_128.out;
      code_low_clamp.fal = rounded_code.out;
      saturated_code.cond = code_gt_max.out;
      saturated_code.tru = 64'd127;
      saturated_code.fal = code_low_clamp.out;
      code_i8_slice.in = saturated_code.out;
      saturated_code_reg.in = saturated_code.out;
      saturated_code_reg.write_en = 1'd1;
      codes_i8.addr0 = entry_address.out;
      codes_i8.content_en = 1'd1;
      codes_i8.write_data = code_i8_slice.out;
      codes_i8.write_en = 1'd1;
      clamp_and_write_code[done] = (saturated_code_reg.done & codes_i8.done) ? 1'd1;
    }
    group multiply_dequant {
      dequant_product.left = saturated_code_reg.out;
      dequant_product.right = output_scale.read_data;
      dequant_product.go = 1'd1;
      multiply_dequant[done] = dequant_product.done;
    }
    group round_and_write_dequant {
      dequant_negative.left = dequant_product.out;
      dequant_negative.right = 64'd0;
      dequant_negate.left = 64'd0;
      dequant_negate.right = dequant_product.out;
      dequant_abs.cond = dequant_negative.out;
      dequant_abs.tru = dequant_negate.out;
      dequant_abs.fal = dequant_product.out;
      dequant_bias.left = dequant_abs.out;
      dequant_bias.right = 64'd128;
      dequant_shift.left = dequant_bias.out;
      dequant_shift.right = 64'd8;
      rounded_dequant_negate.left = 64'd0;
      rounded_dequant_negate.right = dequant_shift.out;
      rounded_dequant.cond = dequant_negative.out;
      rounded_dequant.tru = rounded_dequant_negate.out;
      rounded_dequant.fal = dequant_shift.out;
      q16_16.addr0 = entry_address.out;
      q16_16.content_en = 1'd1;
      q16_16.write_data = rounded_dequant.out;
      q16_16.write_en = 1'd1;
      round_and_write_dequant[done] = q16_16.done;
    }
    group increment_entry_counter {
      increment_entry.left = entry_counter.out;
      increment_entry.right = 9'd1;
      entry_counter.in = increment_entry.out;
      entry_counter.write_en = 1'd1;
      increment_entry_counter[done] = entry_counter.done;
    }
    comb group entry_condition {
      entry_lt.left = entry_counter.out;
      entry_lt.right = 9'd256;
    }
  }
  control {
    seq {
      init_entry;
      while entry_lt.out with entry_condition {
        seq {
          read_inputs;
          multiply_real;
          round_real;
          latch_numerator;
          divide_code;
          clamp_and_write_code;
          multiply_dequant;
          round_and_write_dequant;
          increment_entry_counter;
        }
      }
    }
  }
}
'''


def generate_requantize_kernel(schema_path: Path, fixture_path: Path) -> CalyxArtifact:
    """Authenticate inputs and generate the standalone Task-3 hardware gate."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = _checked_requantize_fixture(schema, fixture_path)
    return CalyxArtifact(
        futil=_requantize_kernel_futil(),
        provenance={
            "generated": "fixed-schema-to-calyx-requantize-sv-v1",
            "schema_receipt_sha256": schema["receipt_sha256"],
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": {
                "schema": schema,
                "schema_receipt_sha256": schema["receipt_sha256"],
                "fixture": _fixture_authority(fixture),
            },
            "memory_shapes": {
                "accumulator_input": [256, 64],
                "weight_scale": [64, 64],
                "output_scale": [64, 64],
                "codes_i8": [256, 8],
                "q16_16": [256, 64],
            },
            "kernel": {
                "entries": 256,
                "product": "signed_i64_twos_complement_wrap",
                "real_q16_16": "signed_magnitude_half_up_shift_32",
                "code": "signed_magnitude_half_up_divide_then_saturate_i8",
                "dequantized_q16_16": "signed_magnitude_half_up_shift_8",
                "requantize_attributes": schema["requantize"],
            },
        },
    )


def _futil_component_sections(futil: str) -> tuple[str, str, str]:
    """Extract cells, wires, and control from one generated main component."""
    cells_marker = "  cells {\n"
    wires_marker = "  }\n  wires {\n"
    control_marker = "  }\n  control {\n"
    cells_start = futil.index(cells_marker) + len(cells_marker)
    wires_start = futil.index(wires_marker, cells_start)
    control_start = futil.index(control_marker, wires_start)
    component_end = futil.rindex("\n  }\n}\n")
    return (
        futil[cells_start:wires_start],
        futil[wires_start + len(wires_marker):control_start],
        futil[control_start + len(control_marker):component_end],
    )


def _composed_slice_kernel_futil() -> str:
    """Compose Task-2 GEMV and Task-3 requantization in one Calyx control."""
    gemv_cells, gemv_wires, gemv_control = _futil_component_sections(
        _full_gemv_kernel_futil()
    )
    requant_cells, requant_wires, requant_control = _futil_component_sections(
        _requantize_kernel_futil()
    )
    accumulator_input = "    @external accumulator_input = seq_mem_d1(64, 256, 8);\n"
    if accumulator_input not in requant_cells:
        raise ValueError("requantization accumulator ABI is missing")
    requant_cells = requant_cells.replace(accumulator_input, "", 1)
    requant_wires = requant_wires.replace("accumulator_input", "accumulator_trace")
    return f'''// Generated Task-4 exact fixed-point composed backend gate.
// The Task-2 GEMV trace memory is the Task-3 requantizer input memory:
// no host or software transfer occurs between the two controls.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
{gemv_cells}{requant_cells}  }}
  wires {{
{gemv_wires}{requant_wires}  }}
  control {{
    seq {{
{gemv_control}
{requant_control}
    }}
  }}
}}
'''


def generate_composed_slice_kernel(schema_path: Path, fixture_path: Path) -> CalyxArtifact:
    """Authenticate inputs and bind GEMV trace directly to requantization."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = _checked_requantize_fixture(schema, fixture_path)
    return CalyxArtifact(
        futil=_composed_slice_kernel_futil(),
        provenance={
            "generated": "fixed-schema-to-calyx-composed-slice-sv-v1",
            "schema_receipt_sha256": schema["receipt_sha256"],
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": {
                "schema": schema,
                "schema_receipt_sha256": schema["receipt_sha256"],
                "fixture": _fixture_authority(fixture),
            },
            "memory_shapes": {
                "activation": [256, 8],
                "input_scale": [64, 64],
                "weights": [4096, 8],
                "accumulator_trace": [256, 64],
                "weight_scale": [64, 64],
                "output_scale": [64, 64],
                "codes_i8": [256, 8],
                "q16_16": [256, 64],
            },
            "composition": {
                "gemv": "fixed-schema-to-calyx-full-gemv-sv-v1",
                "requantize": "fixed-schema-to-calyx-requantize-sv-v1",
                "handoff": "accumulator_trace_external_memory",
                "host_intermediate": False,
                "calyx_control": "single_main_sequential_gemv_then_requantize",
            },
        },
    )


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
        raise RuntimeError(f"unable to build pinned Calyx tool: {completed.stderr.strip()}")
    paths = [Path(line) for line in completed.stdout.splitlines() if line]
    if len(paths) != 1 or not (paths[0] / "bin/calyx").is_file():
        raise RuntimeError("pinned Calyx package did not yield one executable installation")
    return paths[0]


def _cpp_values(values: list[int]) -> str:
    return ", ".join(str(int(value)) for value in values)


def _generated_harness(fixture: dict[str, Any]) -> str:
    activation = [value for row in fixture["tensors"]["activation_codes_i8"]["values"] for value in row]
    input_scale = fixture["tensors"]["input_scale_q8_24"]["values"]
    weights = [value for row in fixture["tensors"]["weight_codes_i8"]["values"] for value in row]
    return f'''// Generated harness: fixture values are preload data, never an output oracle.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

static const std::int64_t kActivation[256] = {{{_cpp_values(activation)}}};
static const std::int64_t kInputScale[64] = {{{_cpp_values(input_scale)}}};
static const std::int64_t kWeights[4096] = {{{_cpp_values(weights)}}};

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
    root->main__DOT__activation__DOT__mem[i] = static_cast<std::uint8_t>(kActivation[i]);
    root->main__DOT__accumulator_trace__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 64; ++i) {{
    root->main__DOT__input_scale__DOT__mem[i] = static_cast<std::uint64_t>(kInputScale[i]);
  }}
  for (unsigned i = 0; i < 4096; ++i) {{
    root->main__DOT__weights__DOT__mem[i] = static_cast<std::uint8_t>(kWeights[i]);
  }}
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 2000) {{
    tick(model);
    ++cycles;
  }}
  const std::int64_t accumulator = static_cast<std::int64_t>(root->main__DOT__accumulator_trace__DOT__mem[0]);
  const bool complete = model.done && cycles > 64;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"accumulator_i64\\\":" << accumulator
            << ",\\\"cycles\\\":" << cycles << "}}\\n";
  return complete ? 0 : 1;
}}
'''


def _generated_full_gemv_harness(fixture: dict[str, Any]) -> str:
    """Generate a harness that reads and hashes every simulator trace word."""
    activation = [value for row in fixture["tensors"]["activation_codes_i8"]["values"] for value in row]
    input_scale = fixture["tensors"]["input_scale_q8_24"]["values"]
    weights = [value for row in fixture["tensors"]["weight_codes_i8"]["values"] for value in row]
    return f'''// Generated harness: fixture values are preload data, never an output oracle.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iomanip>
#include <iostream>

static const std::int64_t kActivation[256] = {{{_cpp_values(activation)}}};
static const std::int64_t kInputScale[64] = {{{_cpp_values(input_scale)}}};
static const std::int64_t kWeights[4096] = {{{_cpp_values(weights)}}};

static std::uint32_t rotr(std::uint32_t value, std::uint32_t count) {{
  return (value >> count) | (value << (32 - count));
}}

class Sha256 {{
 public:
  Sha256() : bit_count_(0), used_(0) {{
    state_[0] = 0x6a09e667U; state_[1] = 0xbb67ae85U;
    state_[2] = 0x3c6ef372U; state_[3] = 0xa54ff53aU;
    state_[4] = 0x510e527fU; state_[5] = 0x9b05688cU;
    state_[6] = 0x1f83d9abU; state_[7] = 0x5be0cd19U;
  }}

  void update(std::uint8_t byte) {{
    block_[used_++] = byte;
    bit_count_ += 8;
    if (used_ == 64) {{ transform(); used_ = 0; }}
  }}

  void final(std::uint8_t digest[32]) {{
    block_[used_++] = 0x80;
    if (used_ > 56) {{
      while (used_ < 64) block_[used_++] = 0;
      transform();
      used_ = 0;
    }}
    while (used_ < 56) block_[used_++] = 0;
    for (unsigned i = 0; i < 8; ++i)
      block_[63 - i] = static_cast<std::uint8_t>(bit_count_ >> (8 * i));
    transform();
    for (unsigned i = 0; i < 8; ++i) {{
      digest[4 * i] = static_cast<std::uint8_t>(state_[i] >> 24);
      digest[4 * i + 1] = static_cast<std::uint8_t>(state_[i] >> 16);
      digest[4 * i + 2] = static_cast<std::uint8_t>(state_[i] >> 8);
      digest[4 * i + 3] = static_cast<std::uint8_t>(state_[i]);
    }}
  }}

 private:
  void transform() {{
    static const std::uint32_t constants[64] = {{
      0x428a2f98U,0x71374491U,0xb5c0fbcfU,0xe9b5dba5U,0x3956c25bU,0x59f111f1U,0x923f82a4U,0xab1c5ed5U,
      0xd807aa98U,0x12835b01U,0x243185beU,0x550c7dc3U,0x72be5d74U,0x80deb1feU,0x9bdc06a7U,0xc19bf174U,
      0xe49b69c1U,0xefbe4786U,0x0fc19dc6U,0x240ca1ccU,0x2de92c6fU,0x4a7484aaU,0x5cb0a9dcU,0x76f988daU,
      0x983e5152U,0xa831c66dU,0xb00327c8U,0xbf597fc7U,0xc6e00bf3U,0xd5a79147U,0x06ca6351U,0x14292967U,
      0x27b70a85U,0x2e1b2138U,0x4d2c6dfcU,0x53380d13U,0x650a7354U,0x766a0abbU,0x81c2c92eU,0x92722c85U,
      0xa2bfe8a1U,0xa81a664bU,0xc24b8b70U,0xc76c51a3U,0xd192e819U,0xd6990624U,0xf40e3585U,0x106aa070U,
      0x19a4c116U,0x1e376c08U,0x2748774cU,0x34b0bcb5U,0x391c0cb3U,0x4ed8aa4aU,0x5b9cca4fU,0x682e6ff3U,
      0x748f82eeU,0x78a5636fU,0x84c87814U,0x8cc70208U,0x90befffaU,0xa4506cebU,0xbef9a3f7U,0xc67178f2U
    }};
    std::uint32_t words[64];
    for (unsigned i = 0; i < 16; ++i)
      words[i] = (static_cast<std::uint32_t>(block_[4 * i]) << 24) |
                 (static_cast<std::uint32_t>(block_[4 * i + 1]) << 16) |
                 (static_cast<std::uint32_t>(block_[4 * i + 2]) << 8) |
                 static_cast<std::uint32_t>(block_[4 * i + 3]);
    for (unsigned i = 16; i < 64; ++i) {{
      const std::uint32_t s0 = rotr(words[i - 15], 7) ^ rotr(words[i - 15], 18) ^ (words[i - 15] >> 3);
      const std::uint32_t s1 = rotr(words[i - 2], 17) ^ rotr(words[i - 2], 19) ^ (words[i - 2] >> 10);
      words[i] = words[i - 16] + s0 + words[i - 7] + s1;
    }}
    std::uint32_t a = state_[0], b = state_[1], c = state_[2], d = state_[3];
    std::uint32_t e = state_[4], f = state_[5], g = state_[6], h = state_[7];
    for (unsigned i = 0; i < 64; ++i) {{
      const std::uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const std::uint32_t choose = (e & f) ^ ((~e) & g);
      const std::uint32_t temp1 = h + s1 + choose + constants[i] + words[i];
      const std::uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const std::uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
      const std::uint32_t temp2 = s0 + majority;
      h = g; g = f; f = e; e = d + temp1;
      d = c; c = b; b = a; a = temp1 + temp2;
    }}
    state_[0] += a; state_[1] += b; state_[2] += c; state_[3] += d;
    state_[4] += e; state_[5] += f; state_[6] += g; state_[7] += h;
  }}

  std::uint32_t state_[8];
  std::uint64_t bit_count_;
  std::uint8_t block_[64];
  unsigned used_;
}};

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
    root->main__DOT__activation__DOT__mem[i] = static_cast<std::uint8_t>(kActivation[i]);
    root->main__DOT__accumulator_trace__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 64; ++i)
    root->main__DOT__input_scale__DOT__mem[i] = static_cast<std::uint64_t>(kInputScale[i]);
  for (unsigned i = 0; i < 4096; ++i)
    root->main__DOT__weights__DOT__mem[i] = static_cast<std::uint8_t>(kWeights[i]);
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 300000) {{
    tick(model);
    ++cycles;
  }}
  std::int64_t trace[256];
  Sha256 hash;
  for (unsigned i = 0; i < 256; ++i) {{
    trace[i] = static_cast<std::int64_t>(root->main__DOT__accumulator_trace__DOT__mem[i]);
    const std::uint64_t raw = static_cast<std::uint64_t>(trace[i]);
    for (unsigned byte = 0; byte < 8; ++byte)
      hash.update(static_cast<std::uint8_t>(raw >> (8 * byte)));
  }}
  std::uint8_t digest[32];
  hash.final(digest);
  const bool complete = model.done && cycles > 4 * 64 * 64;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"accumulator_trace_i64\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << trace[i];
  }}
  std::cout << "],\\\"trace_sha256\\\":\\\"";
  for (unsigned i = 0; i < 32; ++i)
    std::cout << std::hex << std::setfill('0') << std::setw(2) << static_cast<unsigned>(digest[i]);
  std::cout << std::dec << "\\\",\\\"cycles\\\":" << cycles << "}}\\n";
  return complete ? 0 : 1;
}}
'''


def _generated_requantize_harness(fixture: dict[str, Any]) -> str:
    """Generate a harness that supplies only authenticated datapath inputs."""
    accumulators = [value for row in fixture["tensors"]["gemv_accumulator_i64"]["values"] for value in row]
    weight_scale = fixture["tensors"]["weight_scale_q8_24"]["values"]
    output_scale = fixture["tensors"]["output_scale_q8_24"]["values"]
    return f'''// Generated harness: only authenticated input memories are preloaded.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

static const std::int64_t kAccumulator[256] = {{{_cpp_values(accumulators)}}};
static const std::int64_t kWeightScale[64] = {{{_cpp_values(weight_scale)}}};
static const std::int64_t kOutputScale[64] = {{{_cpp_values(output_scale)}}};

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
    root->main__DOT__accumulator_input__DOT__mem[i] = static_cast<std::uint64_t>(kAccumulator[i]);
    root->main__DOT__codes_i8__DOT__mem[i] = 0;
    root->main__DOT__q16_16__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 64; ++i) {{
    root->main__DOT__weight_scale__DOT__mem[i] = static_cast<std::uint64_t>(kWeightScale[i]);
    root->main__DOT__output_scale__DOT__mem[i] = static_cast<std::uint64_t>(kOutputScale[i]);
  }}
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 100000) {{
    tick(model);
    ++cycles;
  }}
  const bool complete = model.done && cycles > 256;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"codes_i8\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<int>(static_cast<std::int8_t>(root->main__DOT__codes_i8__DOT__mem[i]));
  }}
  std::cout << "],\\\"q16_16\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<std::int64_t>(root->main__DOT__q16_16__DOT__mem[i]);
  }}
  std::cout << "],\\\"cycles\\\":" << cycles << "}}\\n";
  return complete ? 0 : 1;
}}
'''


def _generated_composed_slice_harness(fixture: dict[str, Any]) -> str:
    """Generate one harness with no expected or intermediate output arrays."""
    activation = [value for row in fixture["tensors"]["activation_codes_i8"]["values"] for value in row]
    input_scale = fixture["tensors"]["input_scale_q8_24"]["values"]
    weights = [value for row in fixture["tensors"]["weight_codes_i8"]["values"] for value in row]
    weight_scale = fixture["tensors"]["weight_scale_q8_24"]["values"]
    output_scale = fixture["tensors"]["output_scale_q8_24"]["values"]
    return f'''// Generated harness: only authenticated GEMV/scaling inputs are preloaded.
// Accumulators and both requantized memories are observed after one SV execution.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

static const std::int64_t kActivation[256] = {{{_cpp_values(activation)}}};
static const std::int64_t kInputScale[64] = {{{_cpp_values(input_scale)}}};
static const std::int64_t kWeights[4096] = {{{_cpp_values(weights)}}};
static const std::int64_t kWeightScale[64] = {{{_cpp_values(weight_scale)}}};
static const std::int64_t kOutputScale[64] = {{{_cpp_values(output_scale)}}};

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
    root->main__DOT__activation__DOT__mem[i] = static_cast<std::uint8_t>(kActivation[i]);
    root->main__DOT__accumulator_trace__DOT__mem[i] = 0;
    root->main__DOT__codes_i8__DOT__mem[i] = 0;
    root->main__DOT__q16_16__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 64; ++i) {{
    root->main__DOT__input_scale__DOT__mem[i] = static_cast<std::uint64_t>(kInputScale[i]);
    root->main__DOT__weight_scale__DOT__mem[i] = static_cast<std::uint64_t>(kWeightScale[i]);
    root->main__DOT__output_scale__DOT__mem[i] = static_cast<std::uint64_t>(kOutputScale[i]);
  }}
  for (unsigned i = 0; i < 4096; ++i)
    root->main__DOT__weights__DOT__mem[i] = static_cast<std::uint8_t>(kWeights[i]);
  model.reset = 1;
  model.go = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  model.reset = 0;
  model.go = 1;
  unsigned cycles = 0;
  while (!model.done && cycles < 400000) {{
    tick(model);
    ++cycles;
  }}
  const bool complete = model.done && cycles > 4 * 64 * 64 + 256;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"accumulator_trace_i64\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<std::int64_t>(root->main__DOT__accumulator_trace__DOT__mem[i]);
  }}
  std::cout << "],\\\"codes_i8\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<int>(static_cast<std::int8_t>(root->main__DOT__codes_i8__DOT__mem[i]));
  }}
  std::cout << "],\\\"q16_16\\\":[";
  for (unsigned i = 0; i < 256; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<std::int64_t>(root->main__DOT__q16_16__DOT__mem[i]);
  }}
  std::cout << "],\\\"cycles\\\":" << cycles << "}}\\n";
  return complete ? 0 : 1;
}}
'''


def run_generated_sv(artifact: CalyxArtifact, fixture_path: Path, row: int, output: int) -> dict[str, int]:
    """Compile and execute the generated SV, returning its observed trace word."""
    if artifact.provenance.get("generated") != "fixed-schema-to-calyx-one-output-sv-v1":
        raise ValueError("unrecognized one-output generated-SV artifact provenance")
    if (row, output) != (0, 0):
        raise ValueError("Task-1 one-output kernel only supports row=0 and output=0")
    authority = artifact.provenance.get("authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("schema"), dict):
        raise ValueError("generated-SV schema authority binding is missing")
    schema = authority["schema"]
    fixture = _checked_one_output_fixture(schema, fixture_path)
    if (
        authority.get("schema_receipt_sha256") != schema.get("receipt_sha256")
        or artifact.provenance.get("schema_receipt_sha256") != schema.get("receipt_sha256")
    ):
        raise ValueError("generated-SV schema authority mismatch")
    if (
        artifact.provenance.get("fixture_receipt_sha256") != fixture.get("receipt_sha256")
        or authority.get("fixture") != _fixture_authority(fixture)
    ):
        raise ValueError("generated-SV fixture authority mismatch")

    artifact_dir = Path(tempfile.mkdtemp(prefix="fixed-point-calyx-sv-"))
    futil_path = artifact_dir / "one-output.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_harness(fixture), encoding="utf-8")
    calyx = _calyx_install()
    calyx_run = subprocess.run(
        [str(calyx / "bin/calyx"), str(futil_path), "-l", str(calyx / "share/calyx"), "-b", "verilog", "-o", str(sv_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if calyx_run.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"Calyx-to-SV failed: {calyx_run.stderr.strip()}")
    verilator_dir = artifact_dir / "verilator"
    verilator_run = subprocess.run(
        ["verilator", "--cc", "--exe", "--build", "--top-module", "main", "--public-flat-rw", "--Mdir", str(verilator_dir), "-CFLAGS", "-std=c++17", "-o", "fixed_point_harness", str(sv_path), str(harness_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    executable = verilator_dir / "fixed_point_harness"
    if verilator_run.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"Verilator build failed: {verilator_run.stderr.strip()}")
    simulated = subprocess.run([str(executable)], text=True, capture_output=True, check=False, timeout=600)
    if simulated.returncode != 0:
        raise RuntimeError(f"generated-SV harness failed: {simulated.stdout.strip()} {simulated.stderr.strip()}")
    try:
        observed = json.loads(next(line for line in reversed(simulated.stdout.splitlines()) if line.startswith("{")))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"generated-SV harness did not emit JSON: {simulated.stdout!r}") from error
    except StopIteration as error:
        raise RuntimeError(f"generated-SV harness did not emit JSON: {simulated.stdout!r}") from error
    if observed.get("status") != "ok" or not isinstance(observed.get("accumulator_i64"), int) or not isinstance(observed.get("cycles"), int):
        raise RuntimeError(f"generated-SV harness observation is invalid: {observed}")
    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
    }
    return {"accumulator_i64": observed["accumulator_i64"], "cycles": observed["cycles"]}


def run_full_gemv_sv(artifact: CalyxArtifact, fixture_path: Path) -> dict[str, object]:
    """Compile and observe all 256 row-major checkpoints from generated SV."""
    if artifact.provenance.get("generated") != "fixed-schema-to-calyx-full-gemv-sv-v1":
        raise ValueError("unrecognized full-GEMV generated-SV artifact provenance")
    authority = artifact.provenance.get("authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("schema"), dict):
        raise ValueError("generated-SV schema authority binding is missing")
    schema = authority["schema"]
    fixture = _checked_one_output_fixture(schema, fixture_path)
    if (
        authority.get("schema_receipt_sha256") != schema.get("receipt_sha256")
        or artifact.provenance.get("schema_receipt_sha256") != schema.get("receipt_sha256")
    ):
        raise ValueError("generated-SV schema authority mismatch")
    if (
        artifact.provenance.get("fixture_receipt_sha256") != fixture.get("receipt_sha256")
        or authority.get("fixture") != _fixture_authority(fixture)
    ):
        raise ValueError("generated-SV fixture authority mismatch")

    artifact_dir = Path(tempfile.mkdtemp(prefix="fixed-point-calyx-full-gemv-sv-"))
    futil_path = artifact_dir / "full-gemv.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_full_gemv_harness(fixture), encoding="utf-8")
    calyx = _calyx_install()
    calyx_run = subprocess.run(
        [str(calyx / "bin/calyx"), str(futil_path), "-l", str(calyx / "share/calyx"), "-b", "verilog", "-o", str(sv_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if calyx_run.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"Calyx-to-SV failed: {calyx_run.stderr.strip()}")
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    synthesis_run = subprocess.run(
        [str(calyx / "bin/calyx"), str(futil_path), "-l", str(calyx / "share/calyx"), "--synthesis", "--disable-verify", "-b", "verilog", "-o", str(synthesis_sv_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if synthesis_run.returncode != 0 or not synthesis_sv_path.is_file():
        raise RuntimeError(f"Calyx synthesis-to-SV failed: {synthesis_run.stderr.strip()}")
    verilator_dir = artifact_dir / "verilator"
    verilator_run = subprocess.run(
        ["verilator", "--cc", "--exe", "--build", "--top-module", "main", "--public-flat-rw", "--Mdir", str(verilator_dir), "-CFLAGS", "-std=c++17", "-o", "fixed_point_full_gemv_harness", str(sv_path), str(harness_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    executable = verilator_dir / "fixed_point_full_gemv_harness"
    if verilator_run.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"Verilator build failed: {verilator_run.stderr.strip()}")
    simulated = subprocess.run([str(executable)], text=True, capture_output=True, check=False, timeout=600)
    if simulated.returncode != 0:
        raise RuntimeError(f"generated-SV harness failed: {simulated.stdout.strip()} {simulated.stderr.strip()}")
    try:
        observed = json.loads(next(line for line in reversed(simulated.stdout.splitlines()) if line.startswith("{")))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"generated-SV harness did not emit JSON: {simulated.stdout!r}") from error
    except StopIteration as error:
        raise RuntimeError(f"generated-SV harness did not emit JSON: {simulated.stdout!r}") from error
    trace = observed.get("accumulator_trace_i64")
    if (
        observed.get("status") != "ok"
        or not isinstance(trace, list)
        or len(trace) != 256
        or not all(isinstance(value, int) for value in trace)
        or not isinstance(observed.get("trace_sha256"), str)
        or len(observed["trace_sha256"]) != 64
        or not isinstance(observed.get("cycles"), int)
    ):
        raise RuntimeError(f"generated-SV full trace observation is invalid: {observed}")
    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "synthesis_sv": str(synthesis_sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
    }
    return {
        "accumulator_trace_i64": trace,
        "trace_sha256": observed["trace_sha256"],
        "cycles": observed["cycles"],
    }


def run_requantize_sv(artifact: CalyxArtifact, fixture_path: Path) -> dict[str, list[int]]:
    """Compile and observe both standalone requantization result memories."""
    if artifact.provenance.get("generated") != "fixed-schema-to-calyx-requantize-sv-v1":
        raise ValueError("unrecognized requantization generated-SV artifact provenance")
    authority = artifact.provenance.get("authority")
    if not isinstance(authority, dict) or not isinstance(authority.get("schema"), dict):
        raise ValueError("requantization generated-SV schema authority binding is missing")
    schema = authority["schema"]
    fixture = _checked_requantize_fixture(schema, fixture_path)
    if (
        authority.get("schema_receipt_sha256") != schema.get("receipt_sha256")
        or artifact.provenance.get("schema_receipt_sha256") != schema.get("receipt_sha256")
    ):
        raise ValueError("requantization generated-SV schema authority mismatch")
    if (
        artifact.provenance.get("fixture_receipt_sha256") != fixture.get("receipt_sha256")
        or authority.get("fixture") != _fixture_authority(fixture)
    ):
        raise ValueError("requantization generated-SV fixture authority mismatch")

    artifact_dir = Path(tempfile.mkdtemp(prefix="fixed-point-calyx-requantize-sv-"))
    futil_path = artifact_dir / "requantize.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_requantize_harness(fixture), encoding="utf-8")
    calyx = _calyx_install()
    # Cell sharing across these explicitly latched rounding stages creates
    # mux-level combinational cycles in emitted SV; keep this gate unshared.
    calyx_run = subprocess.run(
        [str(calyx / "bin/calyx"), str(futil_path), "-l", str(calyx / "share/calyx"), "-d", "cell-share", "-b", "verilog", "-o", str(sv_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if calyx_run.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"Calyx-to-SV failed: {calyx_run.stderr.strip()}")
    verilator_dir = artifact_dir / "verilator"
    verilator_run = subprocess.run(
        ["verilator", "--cc", "--exe", "--build", "--top-module", "main", "--public-flat-rw", "--Mdir", str(verilator_dir), "-CFLAGS", "-std=c++17", "-o", "fixed_point_requantize_harness", str(sv_path), str(harness_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    executable = verilator_dir / "fixed_point_requantize_harness"
    if verilator_run.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"Verilator build failed: {verilator_run.stderr.strip()}")
    simulated = subprocess.run([str(executable)], text=True, capture_output=True, check=False, timeout=600)
    if simulated.returncode != 0:
        raise RuntimeError(f"generated-SV requantization harness failed: {simulated.stdout.strip()} {simulated.stderr.strip()}")
    try:
        observed = json.loads(next(line for line in reversed(simulated.stdout.splitlines()) if line.startswith("{")))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"generated-SV requantization harness did not emit JSON: {simulated.stdout!r}") from error
    except StopIteration as error:
        raise RuntimeError(f"generated-SV requantization harness did not emit JSON: {simulated.stdout!r}") from error
    codes = observed.get("codes_i8")
    q16 = observed.get("q16_16")
    if (
        observed.get("status") != "ok"
        or not isinstance(codes, list)
        or len(codes) != 256
        or not all(isinstance(value, int) and -128 <= value <= 127 for value in codes)
        or not isinstance(q16, list)
        or len(q16) != 256
        or not all(isinstance(value, int) for value in q16)
        or not isinstance(observed.get("cycles"), int)
    ):
        raise RuntimeError(f"generated-SV requantization observation is invalid: {observed}")
    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
        "cycles": observed["cycles"],
    }
    return {"codes_i8": codes, "q16_16": q16}


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _little_endian_i64_sha256(values: list[int]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(int(value).to_bytes(8, byteorder="little", signed=True))
    return digest.hexdigest()


def _artifact_record(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _require_exact_checkpoints(
    name: str, observed: list[int], expected: list[int]
) -> None:
    if observed == expected:
        return
    mismatch = next(
        (
            (index, actual, wanted)
            for index, (actual, wanted) in enumerate(zip(observed, expected))
            if actual != wanted
        ),
        None,
    )
    if mismatch is None:
        raise RuntimeError(
            f"generated-SV composed {name} length mismatch: "
            f"observed {len(observed)}, expected {len(expected)}"
        )
    index, actual, wanted = mismatch
    raise RuntimeError(
        f"generated-SV composed {name} mismatch at checkpoint {index}: "
        f"observed {actual}, expected {wanted}"
    )


def run_composed_slice_sv(schema_path: Path, fixture_path: Path) -> dict[str, object]:
    """Run GEMV then requantization in one generated Calyx/SV execution."""
    artifact = generate_composed_slice_kernel(schema_path, fixture_path)
    authority = artifact.provenance["authority"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fixture = _checked_requantize_fixture(schema, fixture_path)
    if (
        authority.get("schema") != schema
        or authority.get("schema_receipt_sha256") != schema.get("receipt_sha256")
        or artifact.provenance.get("schema_receipt_sha256") != schema.get("receipt_sha256")
    ):
        raise ValueError("composed generated-SV schema authority mismatch")
    if (
        artifact.provenance.get("fixture_receipt_sha256") != fixture.get("receipt_sha256")
        or authority.get("fixture") != _fixture_authority(fixture)
    ):
        raise ValueError("composed generated-SV fixture authority mismatch")

    # Nix develop sets TMPDIR to a per-invocation directory that is removed on
    # exit.  Keep the receipt's generated paths stable and inspectable across
    # invocations instead of binding its self-hash to that transient directory.
    artifact_dir = Path("/tmp/llm2fpga-fixed-point-gemv-requantize-generated-sv-v1")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "composed.futil"
    sv_path = artifact_dir / "main.sv"
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_composed_slice_harness(fixture), encoding="utf-8")
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
        calyx_command, text=True, capture_output=True, check=False, timeout=600
    )
    if calyx_run.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"composed Calyx-to-SV failed: {calyx_run.stderr.strip()}")
    synthesis_command = [
        str(calyx / "bin/calyx"),
        str(futil_path),
        "-l",
        str(calyx / "share/calyx"),
        "-d",
        "cell-share",
        "--synthesis",
        "--disable-verify",
        "-b",
        "verilog",
        "-o",
        str(synthesis_sv_path),
    ]
    synthesis_run = subprocess.run(
        synthesis_command, text=True, capture_output=True, check=False, timeout=600
    )
    if synthesis_run.returncode != 0 or not synthesis_sv_path.is_file():
        raise RuntimeError(
            f"composed Calyx synthesis-to-SV failed: {synthesis_run.stderr.strip()}"
        )
    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_composed_slice_harness"
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
            executable.name,
            str(sv_path),
            str(harness_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if verilator_run.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"composed Verilator build failed: {verilator_run.stderr.strip()}")
    simulated = subprocess.run(
        [str(executable)], text=True, capture_output=True, check=False, timeout=600
    )
    if simulated.returncode != 0:
        raise RuntimeError(
            "generated-SV composed harness failed: "
            f"{simulated.stdout.strip()} {simulated.stderr.strip()}"
        )
    try:
        observed = json.loads(
            next(line for line in reversed(simulated.stdout.splitlines()) if line.startswith("{"))
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError(
            f"generated-SV composed harness did not emit JSON: {simulated.stdout!r}"
        ) from error
    trace = observed.get("accumulator_trace_i64")
    codes = observed.get("codes_i8")
    q16 = observed.get("q16_16")
    if (
        observed.get("status") != "ok"
        or not isinstance(trace, list)
        or len(trace) != 256
        or not all(isinstance(value, int) for value in trace)
        or not isinstance(codes, list)
        or len(codes) != 256
        or not all(isinstance(value, int) and -128 <= value <= 127 for value in codes)
        or not isinstance(q16, list)
        or len(q16) != 256
        or not all(isinstance(value, int) for value in q16)
        or not isinstance(observed.get("cycles"), int)
    ):
        raise RuntimeError(f"generated-SV composed observation is invalid: {observed}")

    flatten = lambda rows: [value for row in rows for value in row]
    expected_trace = flatten(fixture["tensors"]["gemv_accumulator_i64"]["values"])
    expected_codes = flatten(fixture["tensors"]["requantized_codes_i8"]["values"])
    expected_q16 = flatten(fixture["tensors"]["requantized_q16_16"]["values"])
    _require_exact_checkpoints("accumulator trace", trace, expected_trace)
    _require_exact_checkpoints("requantized i8 codes", codes, expected_codes)
    _require_exact_checkpoints("requantized Q16.16 values", q16, expected_q16)

    trace_sha256 = _little_endian_i64_sha256(trace)
    codes_sha256 = _little_endian_i64_sha256(codes)
    q16_sha256 = _little_endian_i64_sha256(q16)
    for name, actual, record in (
        ("accumulator trace", trace_sha256, fixture["tensors"]["gemv_accumulator_i64"]),
        ("requantized i8 codes", codes_sha256, fixture["tensors"]["requantized_codes_i8"]),
        ("requantized Q16.16 values", q16_sha256, fixture["tensors"]["requantized_q16_16"]),
    ):
        if actual != record["little_endian_int64_sha256"]:
            raise RuntimeError(f"generated-SV composed {name} hash mismatch")

    generated_artifacts = {
        "futil": _artifact_record(futil_path),
        "sv": _artifact_record(sv_path),
        "synthesis_sv": _artifact_record(synthesis_sv_path),
        "harness": _artifact_record(harness_path),
    }
    receipt: dict[str, object] = {
        "schema": "llm2fpga-fixed-point-gemv-requantize-generated-sv-v1",
        "fixture_receipt_sha256": fixture["receipt_sha256"],
        "schema_receipt_sha256": schema["receipt_sha256"],
        "accumulator_trace_sha256": trace_sha256,
        "requantized_codes_sha256": codes_sha256,
        "requantized_q16_16_sha256": q16_sha256,
        "checkpoints": {
            "accumulator_i64": len(trace),
            "requantized_codes_i8": len(codes),
            "requantized_q16_16": len(q16),
        },
        "execution": {
            "calyx_components": 1,
            "simulator_runs": 1,
            "accumulator_handoff": "accumulator_trace_external_memory",
            "host_intermediate": False,
        },
        "cycles": observed["cycles"],
        "generated_artifacts": generated_artifacts,
        "calyx_compile_policy": {
            "disabled_passes": ["cell-share"],
            "reason": "preserve Task-3 staged requantizer latches in composed main",
        },
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _canonical_sha256(unsigned):
        raise RuntimeError("generated-SV composed receipt self-hash mismatch")
    return receipt


def ordered_value_trace(artifact: CalyxArtifact, fixture_path: Path) -> dict[str, Any]:
    """Produce the exact ordered values expected at the generated boundaries."""
    if artifact.provenance.get("generated") != "fixed-schema-to-calyx-v1":
        raise ValueError("unrecognized Calyx artifact provenance")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    codes, _ = activation_qdq(
        _fixture_tensor(fixture, "activation_q16_16"),
        _fixture_tensor(fixture, "input_scale_q8_24"),
    )
    acc = serial_gemv(
        codes * _fixture_tensor(fixture, "input_scale_q8_24"),
        _fixture_tensor(fixture, "weight_codes_i8"),
    )
    real = round_shift_signed(
        acc * _fixture_tensor(fixture, "weight_scale_q8_24"), 2 * Q_SCALE - Q_VALUE
    )
    out, q16 = activation_qdq(real, _fixture_tensor(fixture, "output_scale_q8_24"))
    gemv = [
        {"row": row, "output": output, "accumulator_i64": int(acc[row, output])}
        for row in range(4) for output in range(64)
    ]
    return {
        "schema": "llm2fpga-fixed-schema-calyx-ordered-value-trace-v1",
        "schema_receipt_sha256": artifact.provenance["schema_receipt_sha256"],
        "fixture_receipt_sha256": fixture["receipt_sha256"],
        "gemv": gemv,
        "requantized_codes_i8": out.flatten().tolist(),
        "requantized_q16_16": q16.flatten().tolist(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--futil", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    artifact = lower_schema(args.schema, args.fixture)
    args.futil.write_text(artifact.futil, encoding="utf-8")
    args.trace.write_text(json.dumps(ordered_value_trace(artifact, args.fixture), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.provenance.write_text(json.dumps(artifact.provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
