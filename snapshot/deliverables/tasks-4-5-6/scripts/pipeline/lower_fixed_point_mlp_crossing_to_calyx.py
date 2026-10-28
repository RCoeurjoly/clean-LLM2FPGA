#!/usr/bin/env python3
"""Generate and run the authenticated exact fixed-point GELU hardware gate."""
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


def _channel_address_cells_and_wires(
    prefix: str, count: int, channels: int, *, entry_signal: str | None = None
) -> tuple[list[str], list[str]]:
    """Map a flattened row-major entry to its channel, including 768/3072.

    A low-bit slice is exact only for power-of-two channel counts. For a
    non-power-of-two width, bounded binary subtraction computes the remainder
    without changing the serial schedule or adding a divider.
    """
    if count < channels or channels < 2 or count % channels:
        raise ValueError("channel addressing requires complete positive rows")
    count_width = count.bit_length()
    channel_width = (channels - 1).bit_length()
    entry = entry_signal or f"{prefix}_entry_counter.out"
    address = f"{prefix}_channel_address"
    cells = []
    wires = []
    if channels & (channels - 1):
        previous = entry
        quotient_bits = ((count - 1) // channels).bit_length()
        for bit in reversed(range(quotient_bits)):
            multiple = channels << bit
            ge = f"{prefix}_channel_ge_{bit}"
            subtract = f"{prefix}_channel_subtract_{bit}"
            choose = f"{prefix}_channel_choose_{bit}"
            cells.extend((f"{ge} = std_ge({count_width});",
                          f"{subtract} = std_sub({count_width});",
                          f"{choose} = std_mux({count_width});"))
            wires.extend((
                f"{ge}.left = {previous};",
                f"{ge}.right = {count_width}'d{multiple};",
                f"{subtract}.left = {previous};",
                f"{subtract}.right = {count_width}'d{multiple};",
                f"{choose}.cond = {ge}.out;",
                f"{choose}.tru = {subtract}.out;",
                f"{choose}.fal = {previous};",
            ))
            previous = f"{choose}.out"
    else:
        previous = entry
    cells.append(f"{address} = std_slice({count_width}, {channel_width});")
    wires.append(f"{address}.in = {previous};")
    return cells, wires


@dataclass(frozen=True)
class CalyxArtifact:
    futil: str
    provenance: dict[str, Any]


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capture_module():
    path = ROOT / "TinyStories/capture_fixed_point_mlp_crossing_slice.py"
    spec = importlib.util.spec_from_file_location(
        "fixed_point_mlp_crossing_capture_for_calyx", path
    )
    if spec is None or spec.loader is None:
        raise ValueError("authenticated MLP fixture verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _checked_gelu_fixture(fixture_path: Path) -> dict[str, Any]:
    """Authenticate every fixture record before consuming GELU memories."""
    fixture = _capture_module().verify_fixture(fixture_path)
    if fixture["arithmetic"].get("gelu") != "exact_q12_8192_entry_lut_linear_interpolation":
        raise ValueError("fixture GELU arithmetic authority mismatch")
    expected = {
        "gelu_input_q16_16": ([4, 256], 8192),
        "gelu_lut_q12": ([8192], 65536),
        "gelu_output_q16_16": ([4, 256], 8192),
    }
    for name, (shape, byte_count) in expected.items():
        record = fixture["tensors"][name]
        if record["shape"] != shape or record["bytes"] != byte_count:
            raise ValueError(f"fixture GELU memory contract mismatch: {name}")
    return fixture


def _fixture_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    """Bind generated artifacts to the complete authenticated fixture content."""
    return {
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


def _gelu_kernel_futil() -> str:
    """Emit the exact 1,024-entry LUT/interpolated GELU datapath."""
    return '''// Generated exact fixed-point GELU gate from authenticated fixture authority.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {
  cells {
    @external gelu_input_q16_16 = seq_mem_d1(64, 1024, 10);
    @external gelu_lut_q12 = seq_mem_d1(64, 8192, 13);
    @external gelu_q16_16 = seq_mem_d1(64, 1024, 10);

    entry_counter = std_reg(11);
    entry_lt = std_lt(11);
    increment_entry = std_add(11);
    entry_address = std_slice(11, 10);

    input_negative = std_slt(64);
    input_negate = std_ssub(64);
    input_abs = std_mux(64);
    rounding_bias = std_sadd(64);
    magnitude_shift = std_rsh(64);
    rounded_negate = std_ssub(64);
    rounded = std_mux(64);
    negative_q12_min = std_ssub(64);
    rounded_lt_min = std_slt(64);
    rounded_gt_max = std_sgt(64);
    lower_clamp = std_mux(64);
    q12_clamp = std_mux(64);
    q12 = std_reg(64);

    biased_q12 = std_sadd(64);
    index_shift = std_rsh(64);
    index_slice = std_slice(64, 13);
    fraction_slice = std_slice(64, 3);
    index_is_last = std_eq(13);
    index_increment = std_add(13);
    upper_index = std_mux(13);
    index = std_reg(13);
    upper = std_reg(13);
    fraction = std_reg(3);

    lower_lut = std_reg(64);
    interpolation_delta = std_ssub(64);
    fraction_pad = std_pad(3, 64);
    interpolation_product = std_smult_pipe(64);
    interpolation_shift = std_srsh(64);
    interpolation_sum = std_sadd(64);
    result_shift = std_slsh(64);
  }
  wires {
    entry_address.in = entry_counter.out;

    group init_entry {
      entry_counter.in = 11'd0;
      entry_counter.write_en = 1'd1;
      init_entry[done] = entry_counter.done;
    }
    group read_input {
      gelu_input_q16_16.addr0 = entry_address.out;
      gelu_input_q16_16.content_en = 1'd1;
      read_input[done] = gelu_input_q16_16.done;
    }
    group round_and_clamp_q12 {
      input_negative.left = gelu_input_q16_16.read_data;
      input_negative.right = 64'd0;
      input_negate.left = 64'd0;
      input_negate.right = gelu_input_q16_16.read_data;
      input_abs.cond = input_negative.out;
      input_abs.tru = input_negate.out;
      input_abs.fal = gelu_input_q16_16.read_data;
      rounding_bias.left = input_abs.out;
      rounding_bias.right = 64'd8;
      magnitude_shift.left = rounding_bias.out;
      magnitude_shift.right = 64'd4;
      rounded_negate.left = 64'd0;
      rounded_negate.right = magnitude_shift.out;
      rounded.cond = input_negative.out;
      rounded.tru = rounded_negate.out;
      rounded.fal = magnitude_shift.out;
      negative_q12_min.left = 64'd0;
      negative_q12_min.right = 64'd32768;
      rounded_lt_min.left = rounded.out;
      rounded_lt_min.right = negative_q12_min.out;
      rounded_gt_max.left = rounded.out;
      rounded_gt_max.right = 64'd32767;
      lower_clamp.cond = rounded_lt_min.out;
      lower_clamp.tru = negative_q12_min.out;
      lower_clamp.fal = rounded.out;
      q12_clamp.cond = rounded_gt_max.out;
      q12_clamp.tru = 64'd32767;
      q12_clamp.fal = lower_clamp.out;
      q12.in = q12_clamp.out;
      q12.write_en = 1'd1;
      round_and_clamp_q12[done] = q12.done;
    }
    group latch_lookup_coordinates {
      biased_q12.left = q12.out;
      biased_q12.right = 64'd32768;
      index_shift.left = biased_q12.out;
      index_shift.right = 64'd3;
      index_slice.in = index_shift.out;
      fraction_slice.in = biased_q12.out;
      index_is_last.left = index_slice.out;
      index_is_last.right = 13'd8191;
      index_increment.left = index_slice.out;
      index_increment.right = 13'd1;
      upper_index.cond = index_is_last.out;
      upper_index.tru = 13'd8191;
      upper_index.fal = index_increment.out;
      index.in = index_slice.out;
      index.write_en = 1'd1;
      upper.in = upper_index.out;
      upper.write_en = 1'd1;
      fraction.in = fraction_slice.out;
      fraction.write_en = 1'd1;
      latch_lookup_coordinates[done] = (index.done & upper.done & fraction.done) ? 1'd1;
    }
    group read_lower_lut {
      gelu_lut_q12.addr0 = index.out;
      gelu_lut_q12.content_en = 1'd1;
      read_lower_lut[done] = gelu_lut_q12.done;
    }
    group latch_lower_lut {
      lower_lut.in = gelu_lut_q12.read_data;
      lower_lut.write_en = 1'd1;
      latch_lower_lut[done] = lower_lut.done;
    }
    group read_upper_lut {
      gelu_lut_q12.addr0 = upper.out;
      gelu_lut_q12.content_en = 1'd1;
      read_upper_lut[done] = gelu_lut_q12.done;
    }
    group multiply_interpolation {
      interpolation_delta.left = gelu_lut_q12.read_data;
      interpolation_delta.right = lower_lut.out;
      fraction_pad.in = fraction.out;
      interpolation_product.left = interpolation_delta.out;
      interpolation_product.right = fraction_pad.out;
      interpolation_product.go = 1'd1;
      multiply_interpolation[done] = interpolation_product.done;
    }
    group write_result {
      interpolation_shift.left = interpolation_product.out;
      interpolation_shift.right = 64'd3;
      interpolation_sum.left = lower_lut.out;
      interpolation_sum.right = interpolation_shift.out;
      result_shift.left = interpolation_sum.out;
      result_shift.right = 64'd4;
      gelu_q16_16.addr0 = entry_address.out;
      gelu_q16_16.content_en = 1'd1;
      gelu_q16_16.write_data = result_shift.out;
      gelu_q16_16.write_en = 1'd1;
      write_result[done] = gelu_q16_16.done;
    }
    group increment_entry_counter {
      increment_entry.left = entry_counter.out;
      increment_entry.right = 11'd1;
      entry_counter.in = increment_entry.out;
      entry_counter.write_en = 1'd1;
      increment_entry_counter[done] = entry_counter.done;
    }
    comb group entry_condition {
      entry_lt.left = entry_counter.out;
      entry_lt.right = 11'd1024;
    }
  }
  control {
    seq {
      init_entry;
      while entry_lt.out with entry_condition {
        seq {
          read_input;
          round_and_clamp_q12;
          latch_lookup_coordinates;
          read_lower_lut;
          latch_lower_lut;
          read_upper_lut;
          multiply_interpolation;
          write_result;
          increment_entry_counter;
        }
      }
    }
  }
}
'''


def generate_gelu_kernel(fixture_path: Path) -> CalyxArtifact:
    """Authenticate the MLP fixture and generate the GELU-only Calyx main."""
    fixture = _checked_gelu_fixture(fixture_path)
    futil = _gelu_kernel_futil()
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": "fixed-point-mlp-crossing-gelu-sv-v1",
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "authority": _fixture_authority(fixture),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "host_preload_memories": ["gelu_input_q16_16", "gelu_lut_q12"],
            "calyx_disabled_passes": ["cell-share"],
            "memory_shapes": {
                "gelu_input_q16_16": [1024, 64],
                "gelu_lut_q12": [8192, 64],
                "gelu_q16_16": [1024, 64],
            },
            "kernel": {
                "entries": 1024,
                "round": "signed_magnitude_half_up_shift_4",
                "clamp": [-32768, 32767],
                "lookup": "8192_entry_q12_linear_interpolation_3_fraction_bits",
                "result": "signed_q12_left_shift_4_to_q16_16",
            },
        },
    )


def _cpp_values(values: list[int]) -> str:
    return ", ".join(str(int(value)) for value in values)


def _generated_gelu_harness(fixture: dict[str, Any]) -> str:
    """Preload only authenticated GELU source input and LUT memories."""
    gelu_input = [
        value
        for row in fixture["tensors"]["gelu_input_q16_16"]["values"]
        for value in row
    ]
    gelu_lut = fixture["tensors"]["gelu_lut_q12"]["values"]
    return f'''// Generated harness: preload only authenticated GELU input and LUT.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>

static const std::int64_t kGeluInput[1024] = {{{_cpp_values(gelu_input)}}};
static const std::int64_t kGeluLut[8192] = {{{_cpp_values(gelu_lut)}}};

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
  for (unsigned i = 0; i < 1024; ++i) {{
    root->main__DOT__gelu_input_q16_16__DOT__mem[i] =
        static_cast<std::uint64_t>(kGeluInput[i]);
    root->main__DOT__gelu_q16_16__DOT__mem[i] = 0;
  }}
  for (unsigned i = 0; i < 8192; ++i)
    root->main__DOT__gelu_lut_q12__DOT__mem[i] =
        static_cast<std::uint64_t>(kGeluLut[i]);
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
  const bool complete = model.done && cycles > 1024;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "mismatch")
            << "\\\",\\\"gelu_q16_16\\\":[";
  for (unsigned i = 0; i < 1024; ++i) {{
    if (i) std::cout << ',';
    std::cout << static_cast<std::int64_t>(
        root->main__DOT__gelu_q16_16__DOT__mem[i]);
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


def run_gelu_sv(
    artifact: CalyxArtifact, fixture_path: Path
) -> dict[str, object]:
    """Re-authenticate, compile, and observe every GELU hardware result word."""
    if artifact.provenance.get("generated") != "fixed-point-mlp-crossing-gelu-sv-v1":
        raise ValueError("unrecognized generated-SV GELU artifact provenance")
    fixture = _checked_gelu_fixture(fixture_path)
    if (
        artifact.provenance.get("fixture_receipt_sha256")
        != fixture["receipt_sha256"]
        or artifact.provenance.get("authority") != _fixture_authority(fixture)
    ):
        raise ValueError("generated-SV GELU fixture authority mismatch")
    if artifact.provenance.get("futil_sha256") != hashlib.sha256(
        artifact.futil.encode("utf-8")
    ).hexdigest():
        raise ValueError("generated-SV GELU Futil authority mismatch")

    artifact_dir = Path(tempfile.mkdtemp(prefix="fixed-point-mlp-gelu-sv-"))
    futil_path = artifact_dir / "gelu.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_gelu_harness(fixture), encoding="utf-8")

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
            "fixed_point_mlp_gelu_harness",
            str(sv_path),
            str(harness_path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    executable = verilator_dir / "fixed_point_mlp_gelu_harness"
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
            "generated-SV GELU harness failed: "
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
            f"generated-SV GELU harness did not emit JSON: {simulated.stdout!r}"
        ) from error
    values = observed.get("gelu_q16_16")
    if (
        observed.get("status") != "ok"
        or not isinstance(values, list)
        or len(values) != 1024
        or not all(isinstance(value, int) for value in values)
        or not isinstance(observed.get("cycles"), int)
    ):
        raise RuntimeError(f"generated-SV GELU observation is invalid: {observed}")

    artifact.provenance["generated_sv_artifacts"] = {
        "directory": str(artifact_dir),
        "futil": str(futil_path),
        "sv": str(sv_path),
        "harness": str(harness_path),
        "executable": str(executable),
    }
    artifact.provenance["calyx_command"] = calyx_command
    return {
        "gelu_q16_16": values,
        "little_endian_int64_sha256": _little_endian_i64_sha256(values),
        "cycles": observed["cycles"],
    }


COMPOSED_SCHEMA = "tinystories-1m-fixed-point-mlp-crossing-generated-sv-v1"
COMPOSED_FIXTURE_SCHEMA = "tinystories-1m-fixed-point-mlp-crossing-slice-v1"
COMPOSED_ARTIFACT_DIRECTORY = Path(
    "/tmp/llm2fpga-tinystories-1m-fixed-point-mlp-crossing-generated-sv-v1"
)

COMPOSED_SOURCE_MEMORIES = (
    "c_fc_input_codes_i8",
    "c_fc_input_scale_q8_24",
    "c_fc_weight_codes_i8",
    "c_fc_weight_scale_q8_24",
    "c_fc_bias_q16_16",
    "c_fc_output_scale_q8_24",
    "gelu_lut_q12",
    "c_proj_input_scale_q8_24",
    "c_proj_weight_codes_i8",
    "c_proj_weight_scale_q8_24",
    "c_proj_bias_q16_16",
    "c_proj_output_scale_q8_24",
)

COMPOSED_CHECKPOINT_MEMORIES = (
    "c_fc_input_q16_16",
    "c_fc_accumulator_i64",
    "c_fc_post_weight_rescale_bias_q16_16",
    "c_fc_output_codes_i8",
    "c_fc_output_q16_16",
    "gelu_output_q16_16",
    "c_proj_input_codes_i8",
    "c_proj_input_q16_16",
    "c_proj_accumulator_i64",
    "c_proj_post_weight_rescale_bias_q16_16",
    "c_proj_output_codes_i8",
    "c_proj_output_q16_16",
)


def _checked_composed_fixture(fixture_path: Path) -> dict[str, Any]:
    """Authenticate the exact MLP schema and every generated-memory binding."""
    fixture = _capture_module().verify_fixture(fixture_path)
    if fixture.get("schema") != COMPOSED_FIXTURE_SCHEMA:
        raise ValueError("composed MLP fixture schema authority mismatch")
    if fixture.get("slice") != {
        "layer": 0,
        "c_fc": {"rows": 4, "inputs": 64, "outputs": 256},
        "c_proj": {"rows": 4, "inputs": 256, "outputs": 64},
        "observation_indices": {
            "c_fc_qdq": [24, 30],
            "c_fc_accumulator": 4,
            "gelu_nonlinear": 3,
            "c_proj_qdq": [30, 36],
            "c_proj_accumulator": 5,
        },
    }:
        raise ValueError("composed MLP slice authority mismatch")
    expected_arithmetic = {
        "activation_codes": "signed_int8_saturated",
        "activation_qdq": "signed_int8_saturated_nearest_ties_away_from_zero",
        "gelu": "exact_q12_8192_entry_lut_linear_interpolation",
        "gemv": "ascending_input_index_signed_int64_twos_complement_wrap",
        "post_weight_rescale": "signed_magnitude_half_up_shift_32_plus_q16.16_bias",
        "scale_format": "unsigned_q8.24_int64_tensor",
        "value_format": "signed_q16.16_int64_tensor",
    }
    if fixture.get("arithmetic") != expected_arithmetic:
        raise ValueError("composed MLP arithmetic authority mismatch")
    expected_shapes = {
        "c_fc_input_codes_i8": [4, 64],
        "c_fc_input_scale_q8_24": [64],
        "c_fc_input_q16_16": [4, 64],
        "c_fc_accumulator_i64": [4, 256],
        "c_fc_post_weight_rescale_bias_q16_16": [4, 256],
        "c_fc_output_codes_i8": [4, 256],
        "c_fc_output_scale_q8_24": [256],
        "c_fc_output_q16_16": [4, 256],
        "gelu_input_q16_16": [4, 256],
        "gelu_output_q16_16": [4, 256],
        "gelu_lut_q12": [8192],
        "c_proj_input_codes_i8": [4, 256],
        "c_proj_input_scale_q8_24": [256],
        "c_proj_input_q16_16": [4, 256],
        "c_proj_accumulator_i64": [4, 64],
        "c_proj_post_weight_rescale_bias_q16_16": [4, 64],
        "c_proj_output_codes_i8": [4, 64],
        "c_proj_output_scale_q8_24": [64],
        "c_proj_output_q16_16": [4, 64],
        "c_fc_weight_codes_i8": [256, 64],
        "c_fc_weight_scale_q8_24": [256],
        "c_fc_bias_q16_16": [256],
        "c_proj_weight_codes_i8": [64, 256],
        "c_proj_weight_scale_q8_24": [64],
        "c_proj_bias_q16_16": [64],
    }
    for name, shape in expected_shapes.items():
        record = fixture["tensors"].get(name)
        expected_bytes = 8
        for dimension in shape:
            expected_bytes *= dimension
        if (
            not isinstance(record, dict)
            or record.get("shape") != shape
            or record.get("dtype") != "int64"
            or record.get("bytes") != expected_bytes
            or record.get("fixture_receipt_sha256")
            != fixture["tensor_fixture_receipt_sha256"]
        ):
            raise ValueError(f"composed MLP memory contract mismatch: {name}")
    if (
        fixture["tensors"]["c_fc_output_q16_16"]["little_endian_int64_sha256"]
        != fixture["tensors"]["gelu_input_q16_16"]["little_endian_int64_sha256"]
    ):
        raise ValueError("c_fc-to-GELU direct-memory fixture boundary mismatch")
    return fixture


def _composed_schema_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": COMPOSED_FIXTURE_SCHEMA,
        "layer": 0,
        "prompt_tokens": fixture["prompt_tokens"],
        "c_fc": {"rows": 4, "inputs": 64, "outputs": 256},
        "gelu": "exact_q12_8192_entry_lut_linear_interpolation",
        "c_proj": {"rows": 4, "inputs": 256, "outputs": 64},
        "arithmetic": fixture["arithmetic"],
        "source_memories": list(COMPOSED_SOURCE_MEMORIES),
        "checkpoint_memories": list(COMPOSED_CHECKPOINT_MEMORIES),
        "handoffs": [
            "c_fc_output_q16_16_to_gelu",
            "gelu_output_q16_16_to_c_proj_input_qdq",
        ],
    }
    payload["receipt_sha256"] = _canonical_sha256(payload)
    return payload


def _dequantize_phase(
    prefix: str,
    codes: str,
    scales: str,
    result: str,
    count: int,
    channels: int,
) -> tuple[list[str], list[str], str]:
    count_width = count.bit_length()
    address_width = (count - 1).bit_length()
    channel_cells, channel_wires = _channel_address_cells_and_wires(prefix, count, channels)
    channel_wire_text = "\n    ".join(channel_wires)
    cells = [
        f"{prefix}_entry_counter = std_reg({count_width});",
        f"{prefix}_entry_lt = std_lt({count_width});",
        f"{prefix}_increment_entry = std_add({count_width});",
        f"{prefix}_entry_address = std_slice({count_width}, {address_width});",
        *channel_cells,
        f"{prefix}_code_signed = std_signext(8, 64);",
        f"{prefix}_product = std_smult_pipe(64);",
        f"{prefix}_negative = std_slt(64);",
        f"{prefix}_negate = std_ssub(64);",
        f"{prefix}_absolute = std_mux(64);",
        f"{prefix}_bias = std_sadd(64);",
        f"{prefix}_shift = std_rsh(64);",
        f"{prefix}_rounded_negate = std_ssub(64);",
        f"{prefix}_rounded = std_mux(64);",
    ]
    wires = [
        f"""{prefix}_entry_address.in = {prefix}_entry_counter.out;
    {channel_wire_text}
    group {prefix}_init_entry {{
      {prefix}_entry_counter.in = {count_width}'d0;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_init_entry[done] = {prefix}_entry_counter.done;
    }}
    group {prefix}_read {{
      {codes}.addr0 = {prefix}_entry_address.out;
      {codes}.content_en = 1'd1;
      {scales}.addr0 = {prefix}_channel_address.out;
      {scales}.content_en = 1'd1;
      {prefix}_read[done] = ({codes}.done & {scales}.done) ? 1'd1;
    }}
    group {prefix}_multiply {{
      {prefix}_code_signed.in = {codes}.read_data;
      {prefix}_product.left = {prefix}_code_signed.out;
      {prefix}_product.right = {scales}.read_data;
      {prefix}_product.go = 1'd1;
      {prefix}_multiply[done] = {prefix}_product.done;
    }}
    group {prefix}_round_and_write {{
      {prefix}_negative.left = {prefix}_product.out;
      {prefix}_negative.right = 64'd0;
      {prefix}_negate.left = 64'd0;
      {prefix}_negate.right = {prefix}_product.out;
      {prefix}_absolute.cond = {prefix}_negative.out;
      {prefix}_absolute.tru = {prefix}_negate.out;
      {prefix}_absolute.fal = {prefix}_product.out;
      {prefix}_bias.left = {prefix}_absolute.out;
      {prefix}_bias.right = 64'd128;
      {prefix}_shift.left = {prefix}_bias.out;
      {prefix}_shift.right = 64'd8;
      {prefix}_rounded_negate.left = 64'd0;
      {prefix}_rounded_negate.right = {prefix}_shift.out;
      {prefix}_rounded.cond = {prefix}_negative.out;
      {prefix}_rounded.tru = {prefix}_rounded_negate.out;
      {prefix}_rounded.fal = {prefix}_shift.out;
      {result}.addr0 = {prefix}_entry_address.out;
      {result}.content_en = 1'd1;
      {result}.write_data = {prefix}_rounded.out;
      {result}.write_en = 1'd1;
      {prefix}_round_and_write[done] = {result}.done;
    }}
    group {prefix}_increment_entry_counter {{
      {prefix}_increment_entry.left = {prefix}_entry_counter.out;
      {prefix}_increment_entry.right = {count_width}'d1;
      {prefix}_entry_counter.in = {prefix}_increment_entry.out;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_increment_entry_counter[done] = {prefix}_entry_counter.done;
    }}
    comb group {prefix}_entry_condition {{
      {prefix}_entry_lt.left = {prefix}_entry_counter.out;
      {prefix}_entry_lt.right = {count_width}'d{count};
    }}"""
    ]
    control = f"""      {prefix}_init_entry;
      while {prefix}_entry_lt.out with {prefix}_entry_condition {{
        seq {{ {prefix}_read; {prefix}_multiply; {prefix}_round_and_write; {prefix}_increment_entry_counter; }}
      }}"""
    return cells, wires, control


def _gemv_phase(
    prefix: str,
    codes: str,
    scales: str,
    weights: str,
    accumulator_trace: str,
    rows: int,
    inputs: int,
    outputs: int,
    input_row_expr: str | None = None,
    input_row_width: int | None = None,
    input_address_width_override: int | None = None,
    write_trace: bool = True,
    banked_weights: tuple[str, int] | None = None,
    narrow_products: bool = False,
    packed_weight: bool = False,
    exact_trace_stride: bool = False,
) -> tuple[list[str], list[str], str]:
    row_width = input_row_width or rows.bit_length()
    output_width = outputs.bit_length()
    k_width = inputs.bit_length()
    input_address_width = input_address_width_override or (rows * inputs - 1).bit_length()
    weight_address_width = (outputs * inputs - 1).bit_length()
    trace_address_width = (rows * outputs - 1).bit_length()
    input_shift = (inputs - 1).bit_length()
    trace_shift = (outputs - 1).bit_length()
    product_code_width = 40 if narrow_products else 64
    product_scale_width = 40 if narrow_products else 64
    product_mac_width = 48 if narrow_products else 64
    cells = [
        f"{prefix}_accumulator = std_reg(64);",
        f"{prefix}_row_counter = std_reg({row_width});",
        f"{prefix}_output_counter = std_reg({output_width});",
        f"{prefix}_k_counter = std_reg({k_width});",
        f"{prefix}_row_lt = std_lt({row_width});",
        f"{prefix}_output_lt = std_lt({output_width});",
        f"{prefix}_k_lt = std_lt({k_width});",
        f"{prefix}_increment_row = std_add({row_width});",
        f"{prefix}_increment_output = std_add({output_width});",
        f"{prefix}_increment_k = std_add({k_width});",
        f"{prefix}_input_row_pad = std_pad({row_width}, {input_address_width});",
        f"{prefix}_input_row_shift = std_lsh({input_address_width});",
        f"{prefix}_input_k_pad = std_pad({k_width}, {input_address_width});",
        f"{prefix}_input_address = std_add({input_address_width});",
        f"{prefix}_scale_address = std_slice({k_width}, {(inputs - 1).bit_length()});",
        f"{prefix}_weight_output_pad = std_pad({output_width}, {weight_address_width});",
        f"{prefix}_weight_output_shift = std_lsh({weight_address_width});",
        f"{prefix}_weight_k_pad = std_pad({k_width}, {weight_address_width});",
        f"{prefix}_weight_address = std_add({weight_address_width});",
        f"{prefix}_code_signed = std_signext(8, {product_code_width});",
        f"{prefix}_scale_signed = std_slice(64, {product_scale_width});",
        f"{prefix}_weight_signed = std_signext(8, {product_mac_width});",
        f"{prefix}_activation_scale = std_smult_pipe({product_code_width});",
        f"{prefix}_activation_scale_wide = std_signext({product_code_width}, {product_mac_width});",
        f"{prefix}_mac = std_smult_pipe({product_mac_width});",
        f"{prefix}_mac_wide = std_signext({product_mac_width}, 64);",
        f"{prefix}_add = std_sadd(64);",
    ]
    banked_weight_value = f"{weights}.read_data"
    banked_weight_reads = ""
    banked_weight_done = f"{weights}.done"
    banked_weight_mux_wires = ""
    banked_weight_address_wires = ""
    weight_read_lines = f"      {weights}.addr0 = {prefix}_weight_address.out;\n      {weights}.content_en = 1'd1;\n"
    if banked_weights is not None:
        bank_prefix, bank_count = banked_weights[:2]
        bank_address_shift = banked_weights[2] if len(banked_weights) > 2 else 15
        leaf_count = 1 << (bank_count - 1).bit_length()
        bank_bits = leaf_count.bit_length() - 1
        cells += [
            f"{prefix}_weight_bank_full = std_rsh({weight_address_width});",
            f"{prefix}_weight_bank = std_slice({weight_address_width}, {bank_bits});",
            f"{prefix}_weight_local = std_slice({weight_address_width}, {bank_address_shift});",
        ]
        banked_weight_address_wires = f"      {prefix}_weight_bank_full.left = {prefix}_weight_address.out;\n      {prefix}_weight_bank_full.right = {weight_address_width}'d{bank_address_shift};\n      {prefix}_weight_bank.in = {prefix}_weight_bank_full.out;\n      {prefix}_weight_local.in = {prefix}_weight_address.out;\n"
        banked_weight_done_terms = []
        leaves = []
        for bank in range(bank_count):
            name = f"{bank_prefix}_b{bank}"
            banked_weight_reads += f"      {name}.addr0 = {prefix}_weight_local.out;\n      {name}.content_en = 1'd1;\n"
            banked_weight_done_terms.append(f"{name}.done")
            leaves.append(f"{name}.read_data")
        leaves += [f"{64 if packed_weight else 8}'d0"] * (leaf_count - bank_count)
        for level in range(bank_bits):
            bit = f"{prefix}_weight_bank_bit_{level}"
            # Select this mux level's bank bit; slicing the low bit at every
            # level aliases all banks and silently corrupts weight reads.
            cells.append(f"{bit} = std_bit_slice({bank_bits}, {level}, {level}, 1);")
            banked_weight_mux_wires += f"    {bit}.in = {prefix}_weight_bank.out;\n"
            leaves_next = []
            for index in range(0, len(leaves), 2):
                mux = f"{prefix}_weight_bank_mux_{level}_{index // 2}"
                cells.append(f"{mux} = std_mux({64 if packed_weight else 8});")
                banked_weight_mux_wires += f"    {mux}.cond = {bit}.out; {mux}.tru = {leaves[index + 1]}; {mux}.fal = {leaves[index]};\n"
                leaves_next.append(f"{mux}.out")
            leaves = leaves_next
        raw_banked_weight_value = leaves[0]
        banked_weight_value = raw_banked_weight_value
        banked_weight_done = " & ".join(banked_weight_done_terms)
        weight_read_lines = banked_weight_reads
        if packed_weight:
            cells += [
                f"{prefix}_packed_k = std_rsh({k_width});",
                f"{prefix}_weight_lane = std_slice({k_width}, 3);",
                f"{prefix}_weight_lane_pad = std_pad(3, 64);",
                f"{prefix}_weight_lane_shift = std_lsh(64);",
                f"{prefix}_weight_code_shift = std_rsh(64);",
                f"{prefix}_weight_code_slice = std_slice(64, 8);",
            ]
            banked_weight_value = f"{prefix}_weight_code_slice.out"
    input_row_expr = input_row_expr or f"{prefix}_row_counter.out"
    trace_cells = []
    trace_wires = ""
    write_trace_control = ""
    if write_trace:
        trace_cells = [
            f"{prefix}_trace_row_pad = std_pad({row_width}, {trace_address_width});",
            f"{prefix}_trace_row_shift = std_lsh({trace_address_width});",
            f"{prefix}_trace_output_pad = std_pad({output_width}, {trace_address_width});",
            f"{prefix}_trace_address = std_add({trace_address_width});",
        ]
        trace_wires = f"""group {prefix}_write_accumulator {{
      {prefix}_trace_row_pad.in = {prefix}_row_counter.out;
      {prefix}_trace_row_shift.left = {prefix}_trace_row_pad.out;
      {prefix}_trace_row_shift.right = {trace_address_width}'d{trace_shift};
      {prefix}_trace_output_pad.in = {prefix}_output_counter.out;
      {prefix}_trace_address.left = {prefix}_trace_row_shift.out;
      {prefix}_trace_address.right = {prefix}_trace_output_pad.out;
      {accumulator_trace}.addr0 = {prefix}_trace_address.out;
      {accumulator_trace}.content_en = 1'd1;
      {accumulator_trace}.write_data = {prefix}_accumulator.out;
      {accumulator_trace}.write_en = 1'd1;
      {prefix}_write_accumulator[done] = {accumulator_trace}.done;
    }}"""
        write_trace_control = f"{prefix}_write_accumulator;"
        insert_at = next(i for i, cell in enumerate(cells) if cell.startswith(f"{prefix}_code_signed ="))
        cells[insert_at:insert_at] = trace_cells
    packed_address_assign = ""
    packed_extract_assign = ""
    packed_extract_wires = ""
    weight_k_input = f"{prefix}_k_counter.out"
    if packed_weight:
        packed_address_assign = (
            f"      {prefix}_packed_k.left = {prefix}_k_counter.out;\n"
            f"      {prefix}_packed_k.right = {k_width}'d3;\n"
        )
        packed_extract_assign = (
            f"      {prefix}_weight_lane.in = {prefix}_k_counter.out;\n"
            f"      {prefix}_weight_lane_pad.in = {prefix}_weight_lane.out;\n"
            f"      {prefix}_weight_lane_shift.left = {prefix}_weight_lane_pad.out;\n"
            f"      {prefix}_weight_lane_shift.right = 64'd3;\n"
            f"      {prefix}_weight_code_shift.left = {raw_banked_weight_value};\n"
            f"      {prefix}_weight_code_shift.right = {prefix}_weight_lane_shift.out;\n"
            f"      {prefix}_weight_code_slice.in = {prefix}_weight_code_shift.out;\n"
        )
        # Keep the unpacking combinational.  Calyx's dead-cell pass can drop
        # an all-combinational extraction chain when it is only assigned from
        # a sequential read group, leaving the slice input undriven in SV.
        packed_extract_wires = packed_extract_assign.replace("      ", "    ")
        weight_k_input = f"{prefix}_packed_k.out"
    wires = [
        banked_weight_mux_wires +
        packed_extract_wires +
        f"""group {prefix}_init_row {{
      {prefix}_row_counter.in = {row_width}'d0;
      {prefix}_row_counter.write_en = 1'd1;
      {prefix}_init_row[done] = {prefix}_row_counter.done;
    }}
    group {prefix}_init_output {{
      {prefix}_output_counter.in = {output_width}'d0;
      {prefix}_output_counter.write_en = 1'd1;
      {prefix}_init_output[done] = {prefix}_output_counter.done;
    }}
    group {prefix}_init_accumulator_and_k {{
      {prefix}_accumulator.in = 64'd0;
      {prefix}_accumulator.write_en = 1'd1;
      {prefix}_k_counter.in = {k_width}'d0;
      {prefix}_k_counter.write_en = 1'd1;
      {prefix}_init_accumulator_and_k[done] = ({prefix}_accumulator.done & {prefix}_k_counter.done) ? 1'd1;
    }}
    group {prefix}_read_operands {{
      {prefix}_input_row_pad.in = {input_row_expr};
      {prefix}_input_row_shift.left = {prefix}_input_row_pad.out;
      {prefix}_input_row_shift.right = {input_address_width}'d{input_shift};
      {prefix}_input_k_pad.in = {prefix}_k_counter.out;
      {prefix}_input_address.left = {prefix}_input_row_shift.out;
      {prefix}_input_address.right = {prefix}_input_k_pad.out;
      {prefix}_scale_address.in = {prefix}_k_counter.out;
      {prefix}_weight_output_pad.in = {prefix}_output_counter.out;
      {prefix}_weight_output_shift.left = {prefix}_weight_output_pad.out;
      {prefix}_weight_output_shift.right = {weight_address_width}'d{input_shift - 3 if packed_weight else input_shift};
{packed_address_assign}      {prefix}_weight_k_pad.in = {weight_k_input};
      {prefix}_weight_address.left = {prefix}_weight_output_shift.out;
      {prefix}_weight_address.right = {prefix}_weight_k_pad.out;
{banked_weight_address_wires}      {codes}.addr0 = {prefix}_input_address.out;
      {codes}.content_en = 1'd1;
      {scales}.addr0 = {prefix}_scale_address.out;
      {scales}.content_en = 1'd1;
{weight_read_lines}      {prefix}_read_operands[done] = ({codes}.done & {scales}.done & {banked_weight_done}) ? 1'd1;
    }}
    group {prefix}_scale_activation {{
      {prefix}_code_signed.in = {codes}.read_data;
      {prefix}_activation_scale.left = {prefix}_code_signed.out;
      {prefix}_activation_scale.right = {prefix}_scale_signed.out;
      {prefix}_activation_scale.go = 1'd1;
      {prefix}_scale_activation[done] = {prefix}_activation_scale.done;
    }}
    group {prefix}_multiply {{
      {prefix}_weight_signed.in = {banked_weight_value};
      {prefix}_activation_scale_wide.in = {prefix}_activation_scale.out;
      {prefix}_mac.left = {prefix}_activation_scale_wide.out;
      {prefix}_mac.right = {prefix}_weight_signed.out;
      {prefix}_mac.go = 1'd1;
      {prefix}_multiply[done] = {prefix}_mac.done;
    }}
    group {prefix}_accumulate {{
      {prefix}_add.left = {prefix}_accumulator.out;
      {prefix}_mac_wide.in = {prefix}_mac.out;
      {prefix}_add.right = {prefix}_mac_wide.out;
      {prefix}_accumulator.in = {prefix}_add.out;
      {prefix}_accumulator.write_en = 1'd1;
      {prefix}_accumulate[done] = {prefix}_accumulator.done;
    }}
    group {prefix}_increment_k_counter {{
      {prefix}_increment_k.left = {prefix}_k_counter.out;
      {prefix}_increment_k.right = {k_width}'d1;
      {prefix}_k_counter.in = {prefix}_increment_k.out;
      {prefix}_k_counter.write_en = 1'd1;
      {prefix}_increment_k_counter[done] = {prefix}_k_counter.done;
    }}
    {trace_wires}
    group {prefix}_increment_output_counter {{
      {prefix}_increment_output.left = {prefix}_output_counter.out;
      {prefix}_increment_output.right = {output_width}'d1;
      {prefix}_output_counter.in = {prefix}_increment_output.out;
      {prefix}_output_counter.write_en = 1'd1;
      {prefix}_increment_output_counter[done] = {prefix}_output_counter.done;
    }}
    group {prefix}_increment_row_counter {{
      {prefix}_increment_row.left = {prefix}_row_counter.out;
      {prefix}_increment_row.right = {row_width}'d1;
      {prefix}_row_counter.in = {prefix}_increment_row.out;
      {prefix}_row_counter.write_en = 1'd1;
      {prefix}_increment_row_counter[done] = {prefix}_row_counter.done;
    }}
    comb group {prefix}_row_condition {{
      {prefix}_row_lt.left = {prefix}_row_counter.out;
      {prefix}_row_lt.right = {row_width}'d{rows};
    }}
    comb group {prefix}_output_condition {{
      {prefix}_output_lt.left = {prefix}_output_counter.out;
      {prefix}_output_lt.right = {output_width}'d{outputs};
    }}
    comb group {prefix}_k_condition {{
      {prefix}_k_lt.left = {prefix}_k_counter.out;
      {prefix}_k_lt.right = {k_width}'d{inputs};
    }}"""
    ]
    control = f"""      {prefix}_init_row;
      while {prefix}_row_lt.out with {prefix}_row_condition {{
        seq {{
          {prefix}_init_output;
          while {prefix}_output_lt.out with {prefix}_output_condition {{
            seq {{
              {prefix}_init_accumulator_and_k;
              while {prefix}_k_lt.out with {prefix}_k_condition {{
                seq {{ {prefix}_read_operands; {prefix}_scale_activation; {prefix}_multiply; {prefix}_accumulate; {prefix}_increment_k_counter; }}
              }}
              {write_trace_control}
              {prefix}_increment_output_counter;
            }}
          }}
          {prefix}_increment_row_counter;
        }}
      }}"""
    if narrow_products:
        # Keep the scale adapter driven in the same group as its consumer.
        # The production path otherwise multiplies by an undriven value.
        wires[0] = wires[0].replace(
            f"group {prefix}_scale_activation {{\n",
            f"group {prefix}_scale_activation {{\n      {prefix}_scale_signed.in = {scales}.read_data;\n",
            1,
        )
    else:
        cells = [cell for cell in cells if not any(
            cell.startswith(f"{prefix}_{name} =")
            for name in ("scale_signed", "activation_scale_wide", "mac_wide")
        )]
        wires[0] = wires[0].replace(
            f"{prefix}_activation_scale.right = {prefix}_scale_signed.out;",
            f"{prefix}_activation_scale.right = {scales}.read_data;",
        ).replace(f"      {prefix}_activation_scale_wide.in = {prefix}_activation_scale.out;\n", "").replace(
            f"{prefix}_mac.left = {prefix}_activation_scale_wide.out;",
            f"{prefix}_mac.left = {prefix}_activation_scale.out;",
        ).replace(f"      {prefix}_mac_wide.in = {prefix}_mac.out;\n", "").replace(
            f"{prefix}_add.right = {prefix}_mac_wide.out;",
            f"{prefix}_add.right = {prefix}_mac.out;",
        )
    if banked_weights is not None:
        # The memory read finishes before multiplication consumes the bank
        # mux. Keep its entire address cone driven between those groups;
        # output/k counters do not advance until after accumulator update.
        address_ports = r"(?:output_pad\.in|output_shift\.(?:left|right)|k_pad\.in|address\.(?:left|right)|bank_full\.(?:left|right)|bank\.in|local\.in)"
        pattern = rf"^\s*{re.escape(prefix)}_weight_{address_ports} = [^;]+;"
        assignments = re.findall(pattern, wires[0], flags=re.M)
        if len(assignments) != 10:
            raise ValueError("banked GEMV requires ten weight-address assignments")
        wires[0] = "\n".join(line.strip() for line in assignments) + "\n" + re.sub(pattern, "", wires[0], flags=re.M)

    def exact_stride(value: int, width: int, stem: str, source: str,
                     shift: str, address: str) -> None:
        """Form a 3*power-of-two row stride with shifts/add, not padding."""
        if value & (value - 1) == 0:
            return
        high = 1 << (value.bit_length() - 1)
        low = value - high
        if low & (low - 1):
            raise ValueError(f"{stem}: unsupported non-power-of-two GEMV stride {value}")
        low_cell = f"{prefix}_{stem}_low_shift"
        sum_cell = f"{prefix}_{stem}_stride"
        cells.extend([f"{low_cell} = std_lsh({width});",
                      f"{sum_cell} = std_add({width});"])
        old_shift = f"{shift}.right = {width}'d{(value - 1).bit_length()};"
        old_address = f"{address}.left = {shift}.out;"
        if wires[0].count(old_shift) != 1 or wires[0].count(old_address) != 1:
            raise ValueError(f"{stem}: GEMV address ABI changed")
        wires[0] = wires[0].replace(old_shift, f"{shift}.right = {width}'d{high.bit_length() - 1};")
        wires[0] = wires[0].replace(old_address, f"{address}.left = {sum_cell}.out;")
        wires[0] += (f"\n{low_cell}.left = {source}.out;"
                     f" {low_cell}.right = {width}'d{low.bit_length() - 1};"
                     f" {sum_cell}.left = {shift}.out;"
                     f" {sum_cell}.right = {low_cell}.out;")

    exact_stride(inputs, input_address_width, "input_row", f"{prefix}_input_row_pad",
                 f"{prefix}_input_row_shift", f"{prefix}_input_address")
    weight_stride = inputs // 8 if packed_weight else inputs
    exact_stride(weight_stride, weight_address_width, "weight_output",
                 f"{prefix}_weight_output_pad", f"{prefix}_weight_output_shift",
                 f"{prefix}_weight_address")
    if write_trace and exact_trace_stride:
        exact_stride(outputs, trace_address_width, "trace_row",
                     f"{prefix}_trace_row_pad", f"{prefix}_trace_row_shift",
                     f"{prefix}_trace_address")
    return cells, wires, control


def _activation_qdq_phase(
    prefix: str,
    values_q16: str,
    scales_q24: str,
    codes_i8: str,
    dequantized_q16: str,
    count: int,
    channels: int,
    narrow_products: bool = False,
) -> tuple[list[str], list[str], str]:
    count_width = count.bit_length()
    address_width = (count - 1).bit_length()
    channel_cells, channel_wires = _channel_address_cells_and_wires(prefix, count, channels)
    channel_wire_text = "\n    ".join(channel_wires)
    dequant_product_width = 40 if narrow_products else 64
    cells = [
        f"{prefix}_entry_counter = std_reg({count_width});",
        f"{prefix}_entry_lt = std_lt({count_width});",
        f"{prefix}_increment_entry = std_add({count_width});",
        f"{prefix}_entry_address = std_slice({count_width}, {address_width});",
        *channel_cells,
        f"{prefix}_numerator_lshift = std_lsh(64);",
        f"{prefix}_numerator_negative = std_slt(64);",
        f"{prefix}_numerator = std_reg(64);",
        f"{prefix}_negative_flag = std_reg(1);",
        f"{prefix}_numerator_negate = std_ssub(64);",
        f"{prefix}_numerator_abs = std_mux(64);",
        f"{prefix}_scale_half_bits = std_bit_slice(64, 1, 63, 63);",
        f"{prefix}_scale_half = std_pad(63, 64);",
        f"{prefix}_numerator_bias = std_sadd(64);",
        f"{prefix}_divide = std_div_pipe(64);",
        f"{prefix}_quotient_negate = std_ssub(64);",
        f"{prefix}_rounded_code = std_mux(64);",
        f"{prefix}_negative_128 = std_ssub(64);",
        f"{prefix}_code_lt_min = std_slt(64);",
        f"{prefix}_code_gt_max = std_sgt(64);",
        f"{prefix}_low_clamp = std_mux(64);",
        f"{prefix}_saturated_code = std_mux(64);",
        f"{prefix}_code_slice = std_slice(64, 8);",
        f"{prefix}_code_reg = std_reg(64);",
        f"{prefix}_dequant_code = std_slice(64, {dequant_product_width});",
        f"{prefix}_dequant_scale = std_slice(64, {dequant_product_width});",
        f"{prefix}_dequant_product = std_smult_pipe({dequant_product_width});",
        f"{prefix}_dequant_product_wide = std_signext({dequant_product_width}, 64);",
        f"{prefix}_dequant_negative = std_slt(64);",
        f"{prefix}_dequant_negate = std_ssub(64);",
        f"{prefix}_dequant_abs = std_mux(64);",
        f"{prefix}_dequant_bias = std_sadd(64);",
        f"{prefix}_dequant_shift = std_rsh(64);",
        f"{prefix}_rounded_dequant_negate = std_ssub(64);",
        f"{prefix}_rounded_dequant = std_mux(64);",
    ]
    wires = [
        f"""{prefix}_entry_address.in = {prefix}_entry_counter.out;
    {channel_wire_text}
    group {prefix}_init_entry {{
      {prefix}_entry_counter.in = {count_width}'d0;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_init_entry[done] = {prefix}_entry_counter.done;
    }}
    group {prefix}_read_inputs {{
      {values_q16}.addr0 = {prefix}_entry_address.out;
      {values_q16}.content_en = 1'd1;
      {scales_q24}.addr0 = {prefix}_channel_address.out;
      {scales_q24}.content_en = 1'd1;
      {prefix}_read_inputs[done] = ({values_q16}.done & {scales_q24}.done) ? 1'd1;
    }}
    group {prefix}_latch_numerator {{
      {prefix}_numerator_lshift.left = {values_q16}.read_data;
      {prefix}_numerator_lshift.right = 64'd8;
      {prefix}_numerator_negative.left = {prefix}_numerator_lshift.out;
      {prefix}_numerator_negative.right = 64'd0;
      {prefix}_numerator.in = {prefix}_numerator_lshift.out;
      {prefix}_numerator.write_en = 1'd1;
      {prefix}_negative_flag.in = {prefix}_numerator_negative.out;
      {prefix}_negative_flag.write_en = 1'd1;
      {prefix}_latch_numerator[done] = ({prefix}_numerator.done & {prefix}_negative_flag.done) ? 1'd1;
    }}
    group {prefix}_divide_code {{
      {prefix}_numerator_negate.left = 64'd0;
      {prefix}_numerator_negate.right = {prefix}_numerator.out;
      {prefix}_numerator_abs.cond = {prefix}_negative_flag.out;
      {prefix}_numerator_abs.tru = {prefix}_numerator_negate.out;
      {prefix}_numerator_abs.fal = {prefix}_numerator.out;
      {prefix}_scale_half_bits.in = {scales_q24}.read_data;
      {prefix}_scale_half.in = {prefix}_scale_half_bits.out;
      {prefix}_numerator_bias.left = {prefix}_numerator_abs.out;
      {prefix}_numerator_bias.right = {prefix}_scale_half.out;
      {prefix}_divide.left = {prefix}_numerator_bias.out;
      {prefix}_divide.right = {scales_q24}.read_data;
      {prefix}_divide.go = 1'd1;
      {prefix}_divide_code[done] = {prefix}_divide.done;
    }}
    group {prefix}_clamp_and_write_code {{
      {prefix}_quotient_negate.left = 64'd0;
      {prefix}_quotient_negate.right = {prefix}_divide.out_quotient;
      {prefix}_rounded_code.cond = {prefix}_negative_flag.out;
      {prefix}_rounded_code.tru = {prefix}_quotient_negate.out;
      {prefix}_rounded_code.fal = {prefix}_divide.out_quotient;
      {prefix}_negative_128.left = 64'd0;
      {prefix}_negative_128.right = 64'd128;
      {prefix}_code_lt_min.left = {prefix}_rounded_code.out;
      {prefix}_code_lt_min.right = {prefix}_negative_128.out;
      {prefix}_code_gt_max.left = {prefix}_rounded_code.out;
      {prefix}_code_gt_max.right = 64'd127;
      {prefix}_low_clamp.cond = {prefix}_code_lt_min.out;
      {prefix}_low_clamp.tru = {prefix}_negative_128.out;
      {prefix}_low_clamp.fal = {prefix}_rounded_code.out;
      {prefix}_saturated_code.cond = {prefix}_code_gt_max.out;
      {prefix}_saturated_code.tru = 64'd127;
      {prefix}_saturated_code.fal = {prefix}_low_clamp.out;
      {prefix}_code_slice.in = {prefix}_saturated_code.out;
      {prefix}_code_reg.in = {prefix}_saturated_code.out;
      {prefix}_code_reg.write_en = 1'd1;
      {codes_i8}.addr0 = {prefix}_entry_address.out;
      {codes_i8}.content_en = 1'd1;
      {codes_i8}.write_data = {prefix}_code_slice.out;
      {codes_i8}.write_en = 1'd1;
      {prefix}_clamp_and_write_code[done] = ({prefix}_code_reg.done & {codes_i8}.done) ? 1'd1;
    }}
    group {prefix}_multiply_dequant {{
      {prefix}_dequant_code.in = {prefix}_code_reg.out;
      {prefix}_dequant_scale.in = {scales_q24}.read_data;
      {prefix}_dequant_product.left = {prefix}_dequant_code.out;
      {prefix}_dequant_product.right = {prefix}_dequant_scale.out;
      {prefix}_dequant_product.go = 1'd1;
      {prefix}_multiply_dequant[done] = {prefix}_dequant_product.done;
    }}
    group {prefix}_round_and_write_dequant {{
      {prefix}_dequant_product_wide.in = {prefix}_dequant_product.out;
      {prefix}_dequant_negative.left = {prefix}_dequant_product_wide.out;
      {prefix}_dequant_negative.right = 64'd0;
      {prefix}_dequant_negate.left = 64'd0;
      {prefix}_dequant_negate.right = {prefix}_dequant_product_wide.out;
      {prefix}_dequant_abs.cond = {prefix}_dequant_negative.out;
      {prefix}_dequant_abs.tru = {prefix}_dequant_negate.out;
      {prefix}_dequant_abs.fal = {prefix}_dequant_product_wide.out;
      {prefix}_dequant_bias.left = {prefix}_dequant_abs.out;
      {prefix}_dequant_bias.right = 64'd128;
      {prefix}_dequant_shift.left = {prefix}_dequant_bias.out;
      {prefix}_dequant_shift.right = 64'd8;
      {prefix}_rounded_dequant_negate.left = 64'd0;
      {prefix}_rounded_dequant_negate.right = {prefix}_dequant_shift.out;
      {prefix}_rounded_dequant.cond = {prefix}_dequant_negative.out;
      {prefix}_rounded_dequant.tru = {prefix}_rounded_dequant_negate.out;
      {prefix}_rounded_dequant.fal = {prefix}_dequant_shift.out;
      {dequantized_q16}.addr0 = {prefix}_entry_address.out;
      {dequantized_q16}.content_en = 1'd1;
      {dequantized_q16}.write_data = {prefix}_rounded_dequant.out;
      {dequantized_q16}.write_en = 1'd1;
      {prefix}_round_and_write_dequant[done] = {dequantized_q16}.done;
    }}
    group {prefix}_increment_entry_counter {{
      {prefix}_increment_entry.left = {prefix}_entry_counter.out;
      {prefix}_increment_entry.right = {count_width}'d1;
      {prefix}_entry_counter.in = {prefix}_increment_entry.out;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_increment_entry_counter[done] = {prefix}_entry_counter.done;
    }}
    comb group {prefix}_entry_condition {{
      {prefix}_entry_lt.left = {prefix}_entry_counter.out;
      {prefix}_entry_lt.right = {count_width}'d{count};
    }}"""
    ]
    control = f"""      {prefix}_init_entry;
      while {prefix}_entry_lt.out with {prefix}_entry_condition {{
        seq {{
          {prefix}_read_inputs;
          {prefix}_latch_numerator;
          {prefix}_divide_code;
          {prefix}_clamp_and_write_code;
          {prefix}_multiply_dequant;
          {prefix}_round_and_write_dequant;
          {prefix}_increment_entry_counter;
        }}
      }}"""
    if not narrow_products:
        cells = [cell for cell in cells if not any(
            cell.startswith(f"{prefix}_{name} =")
            for name in ("dequant_code", "dequant_scale", "dequant_product_wide")
        )]
        wires[0] = wires[0].replace(
            f"      {prefix}_dequant_code.in = {prefix}_code_reg.out;\n", ""
        ).replace(
            f"      {prefix}_dequant_scale.in = {scales_q24}.read_data;\n", ""
        ).replace(
            f"      {prefix}_dequant_product.left = {prefix}_dequant_code.out;",
            f"      {prefix}_dequant_product.left = {prefix}_code_reg.out;",
        ).replace(
            f"      {prefix}_dequant_product.right = {prefix}_dequant_scale.out;",
            f"      {prefix}_dequant_product.right = {scales_q24}.read_data;",
        ).replace(
            f"      {prefix}_dequant_product_wide.in = {prefix}_dequant_product.out;\n", ""
        ).replace(
            f"{prefix}_dequant_negative.left = {prefix}_dequant_product_wide.out;",
            f"{prefix}_dequant_negative.left = {prefix}_dequant_product.out;",
        ).replace(
            f"{prefix}_dequant_negate.right = {prefix}_dequant_product_wide.out;",
            f"{prefix}_dequant_negate.right = {prefix}_dequant_product.out;",
        ).replace(
            f"{prefix}_dequant_abs.fal = {prefix}_dequant_product_wide.out;",
            f"{prefix}_dequant_abs.fal = {prefix}_dequant_product.out;",
        )
    return cells, wires, control


def _requantize_phase(
    prefix: str,
    accumulators: str,
    weight_scales: str,
    biases: str,
    output_scales: str,
    post_q16: str,
    codes_i8: str,
    dequantized_q16: str,
    count: int,
    channels: int,
    narrow_products: bool = False,
    requantization_width: int | None = None,
) -> tuple[list[str], list[str], str]:
    count_width = count.bit_length()
    address_width = (count - 1).bit_length()
    channel_cells, channel_wires = _channel_address_cells_and_wires(prefix, count, channels)
    channel_wire_text = "\n    ".join(channel_wires)
    # The authenticated reachable-domain certificate bounds every
    # accumulator*Q8.24-scale product below 2**52.  Keep the diagnostic
    # default at 64 bits, but use a signed 53-bit post-scale datapath in the
    # production model and explicitly restore the 64-bit memory ABI.
    product_width = requantization_width if requantization_width is not None else (53 if narrow_products else 64)
    if product_width not in (53, 64):
        raise ValueError("requantization width must be 53 or 64")
    cells = [
        f"{prefix}_entry_counter = std_reg({count_width});",
        f"{prefix}_entry_lt = std_lt({count_width});",
        f"{prefix}_increment_entry = std_add({count_width});",
        f"{prefix}_entry_address = std_slice({count_width}, {address_width});",
        *channel_cells,
        f"{prefix}_real_product = std_smult_pipe({product_width});",
        f"{prefix}_product_negative = std_slt({product_width});",
        f"{prefix}_product_negate = std_ssub({product_width});",
        f"{prefix}_product_abs = std_mux({product_width});",
        f"{prefix}_product_bias = std_sadd({product_width});",
        f"{prefix}_product_shift = std_rsh({product_width});",
        f"{prefix}_rounded_product_negate = std_ssub({product_width});",
        f"{prefix}_rounded_product = std_mux({product_width});",
        f"{prefix}_real_q16 = std_reg(64);",
        f"{prefix}_bias_add = std_sadd(64);",
        f"{prefix}_post_q16 = std_reg(64);",
    ]
    qdq_cells, qdq_wires, qdq_control = _activation_qdq_phase(
        f"{prefix}_qdq",
        post_q16,
        output_scales,
        codes_i8,
        dequantized_q16,
        count,
        channels,
    )
    # The Q/DQ phase owns its own traversal. The rescale phase first populates
    # post_q16 for all entries; the second traversal consumes that hardware memory.
    cells.extend(qdq_cells)
    if narrow_products:
        cells.extend([
            f"{prefix}_rounded_product_wide = std_signext({product_width}, 64);",
            f"{prefix}_accumulator_product = std_slice(64, {product_width});",
            f"{prefix}_scale_product = std_slice(64, {product_width});",
        ])
        product_left = f"{prefix}_accumulator_product.out"
        product_right = f"{prefix}_scale_product.out"
        rounded_product = f"{prefix}_rounded_product_wide.out"
        input_adapters = (
            f"      {prefix}_accumulator_product.in = {accumulators}.read_data;\n"
            f"      {prefix}_scale_product.in = {weight_scales}.read_data;\n"
        )
    else:
        product_left = f"{accumulators}.read_data"
        product_right = f"{weight_scales}.read_data"
        rounded_product = f"{prefix}_rounded_product.out"
        input_adapters = ""
    multiply_inputs = (
        f"{input_adapters}      {prefix}_real_product.left = {product_left};\n"
        f"      {prefix}_real_product.right = {product_right};"
    )
    wires = [
        f"""{prefix}_entry_address.in = {prefix}_entry_counter.out;
    {channel_wire_text}
    group {prefix}_init_entry {{
      {prefix}_entry_counter.in = {count_width}'d0;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_init_entry[done] = {prefix}_entry_counter.done;
    }}
    group {prefix}_read_inputs {{
      {accumulators}.addr0 = {prefix}_entry_address.out;
      {accumulators}.content_en = 1'd1;
      {weight_scales}.addr0 = {prefix}_channel_address.out;
      {weight_scales}.content_en = 1'd1;
      {biases}.addr0 = {prefix}_channel_address.out;
      {biases}.content_en = 1'd1;
      {prefix}_read_inputs[done] = ({accumulators}.done & {weight_scales}.done & {biases}.done) ? 1'd1;
    }}
    group {prefix}_multiply_real {{
{multiply_inputs}
      {prefix}_real_product.go = 1'd1;
      {prefix}_multiply_real[done] = {prefix}_real_product.done;
    }}
    group {prefix}_round_real {{
      {prefix}_product_negative.left = {prefix}_real_product.out;
      {prefix}_product_negative.right = {product_width}'d0;
      {prefix}_product_negate.left = {product_width}'d0;
      {prefix}_product_negate.right = {prefix}_real_product.out;
      {prefix}_product_abs.cond = {prefix}_product_negative.out;
      {prefix}_product_abs.tru = {prefix}_product_negate.out;
      {prefix}_product_abs.fal = {prefix}_real_product.out;
      {prefix}_product_bias.left = {prefix}_product_abs.out;
      {prefix}_product_bias.right = {product_width}'d2147483648;
      {prefix}_product_shift.left = {prefix}_product_bias.out;
      {prefix}_product_shift.right = {product_width}'d32;
      {prefix}_rounded_product_negate.left = {product_width}'d0;
      {prefix}_rounded_product_negate.right = {prefix}_product_shift.out;
      {prefix}_rounded_product.cond = {prefix}_product_negative.out;
      {prefix}_rounded_product.tru = {prefix}_rounded_product_negate.out;
      {prefix}_rounded_product.fal = {prefix}_product_shift.out;
      {f'{prefix}_rounded_product_wide.in = {prefix}_rounded_product.out;' if narrow_products else ''}
      {prefix}_real_q16.in = {rounded_product};
      {prefix}_real_q16.write_en = 1'd1;
      {prefix}_round_real[done] = {prefix}_real_q16.done;
    }}
    group {prefix}_add_bias_and_write {{
      {prefix}_bias_add.left = {prefix}_real_q16.out;
      {prefix}_bias_add.right = {biases}.read_data;
      {prefix}_post_q16.in = {prefix}_bias_add.out;
      {prefix}_post_q16.write_en = 1'd1;
      {post_q16}.addr0 = {prefix}_entry_address.out;
      {post_q16}.content_en = 1'd1;
      {post_q16}.write_data = {prefix}_bias_add.out;
      {post_q16}.write_en = 1'd1;
      {prefix}_add_bias_and_write[done] = ({prefix}_post_q16.done & {post_q16}.done) ? 1'd1;
    }}
    group {prefix}_increment_entry_counter {{
      {prefix}_increment_entry.left = {prefix}_entry_counter.out;
      {prefix}_increment_entry.right = {count_width}'d1;
      {prefix}_entry_counter.in = {prefix}_increment_entry.out;
      {prefix}_entry_counter.write_en = 1'd1;
      {prefix}_increment_entry_counter[done] = {prefix}_entry_counter.done;
    }}
    comb group {prefix}_entry_condition {{
      {prefix}_entry_lt.left = {prefix}_entry_counter.out;
      {prefix}_entry_lt.right = {count_width}'d{count};
    }}""",
        *qdq_wires,
    ]
    control = f"""      {prefix}_init_entry;
      while {prefix}_entry_lt.out with {prefix}_entry_condition {{
        seq {{
          {prefix}_read_inputs;
          {prefix}_multiply_real;
          {prefix}_round_real;
          {prefix}_add_bias_and_write;
          {prefix}_increment_entry_counter;
        }}
      }}
{qdq_control}"""
    return cells, wires, control


def _component_sections(futil: str) -> tuple[str, str, str]:
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


def _gelu_phase() -> tuple[list[str], list[str], str]:
    """Namespace the already-proven exact GELU kernel for direct handoff."""
    cells_text, wires_text, control_text = _component_sections(_gelu_kernel_futil())
    external_lines = (
        "    @external gelu_input_q16_16 = seq_mem_d1(64, 1024, 10);\n",
        "    @external gelu_lut_q12 = seq_mem_d1(64, 8192, 13);\n",
        "    @external gelu_q16_16 = seq_mem_d1(64, 1024, 10);\n",
    )
    for line in external_lines:
        if line not in cells_text:
            raise ValueError("proven GELU memory ABI changed")
        cells_text = cells_text.replace(line, "", 1)

    cell_names = re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", cells_text, re.MULTILINE)
    group_names = re.findall(
        r"^\s*(?:comb\s+)?group\s+([A-Za-z_][A-Za-z0-9_]*)",
        wires_text,
        re.MULTILINE,
    )
    replacements = {
        "gelu_input_q16_16": "c_fc_output_q16_16",
        "gelu_q16_16": "gelu_output_q16_16",
    }
    replacements.update({name: f"gelu_{name}" for name in cell_names + group_names})
    for old in sorted(replacements, key=len, reverse=True):
        replacement = replacements[old]
        cells_text = re.sub(rf"\b{re.escape(old)}\b", replacement, cells_text)
        wires_text = re.sub(rf"\b{re.escape(old)}\b", replacement, wires_text)
        control_text = re.sub(rf"\b{re.escape(old)}\b", replacement, control_text)
    cells = [line.strip() for line in cells_text.splitlines() if line.strip()]
    return cells, [wires_text.strip()], control_text.strip()


def _indent_lines(lines: list[str], spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(
        prefix + line if line else "" for item in lines for line in item.splitlines()
    )


def _composed_mlp_kernel_futil() -> str:
    """Emit one exact MLP-crossing `main` with only hardware-memory handoff."""
    memory_cells = [
        "@external c_fc_input_codes_i8 = seq_mem_d1(8, 256, 8);",
        "@external c_fc_input_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external c_fc_input_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external c_fc_weight_codes_i8 = seq_mem_d1(8, 16384, 14);",
        "@external c_fc_weight_scale_q8_24 = seq_mem_d1(64, 256, 8);",
        "@external c_fc_bias_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external c_fc_accumulator_i64 = seq_mem_d1(64, 1024, 10);",
        "@external c_fc_post_weight_rescale_bias_q16_16 = seq_mem_d1(64, 1024, 10);",
        "@external c_fc_output_scale_q8_24 = seq_mem_d1(64, 256, 8);",
        "@external c_fc_output_codes_i8 = seq_mem_d1(8, 1024, 10);",
        "@external c_fc_output_q16_16 = seq_mem_d1(64, 1024, 10);",
        "@external gelu_lut_q12 = seq_mem_d1(64, 8192, 13);",
        "@external gelu_output_q16_16 = seq_mem_d1(64, 1024, 10);",
        "@external c_proj_input_scale_q8_24 = seq_mem_d1(64, 256, 8);",
        "@external c_proj_input_codes_i8 = seq_mem_d1(8, 1024, 10);",
        "@external c_proj_input_q16_16 = seq_mem_d1(64, 1024, 10);",
        "@external c_proj_weight_codes_i8 = seq_mem_d1(8, 16384, 14);",
        "@external c_proj_weight_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external c_proj_bias_q16_16 = seq_mem_d1(64, 64, 6);",
        "@external c_proj_accumulator_i64 = seq_mem_d1(64, 256, 8);",
        "@external c_proj_post_weight_rescale_bias_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external c_proj_output_scale_q8_24 = seq_mem_d1(64, 64, 6);",
        "@external c_proj_output_codes_i8 = seq_mem_d1(8, 256, 8);",
        "@external c_proj_output_q16_16 = seq_mem_d1(64, 256, 8);",
    ]
    phases = [
        _dequantize_phase(
            "c_fc_input_dequant",
            "c_fc_input_codes_i8",
            "c_fc_input_scale_q8_24",
            "c_fc_input_q16_16",
            256,
            64,
        ),
        _gemv_phase(
            "c_fc_gemv",
            "c_fc_input_codes_i8",
            "c_fc_input_scale_q8_24",
            "c_fc_weight_codes_i8",
            "c_fc_accumulator_i64",
            4,
            64,
            256,
        ),
        _requantize_phase(
            "c_fc_requant",
            "c_fc_accumulator_i64",
            "c_fc_weight_scale_q8_24",
            "c_fc_bias_q16_16",
            "c_fc_output_scale_q8_24",
            "c_fc_post_weight_rescale_bias_q16_16",
            "c_fc_output_codes_i8",
            "c_fc_output_q16_16",
            1024,
            256,
        ),
        _gelu_phase(),
        _activation_qdq_phase(
            "c_proj_input_qdq",
            "gelu_output_q16_16",
            "c_proj_input_scale_q8_24",
            "c_proj_input_codes_i8",
            "c_proj_input_q16_16",
            1024,
            256,
        ),
        _gemv_phase(
            "c_proj_gemv",
            "c_proj_input_codes_i8",
            "c_proj_input_scale_q8_24",
            "c_proj_weight_codes_i8",
            "c_proj_accumulator_i64",
            4,
            256,
            64,
        ),
        _requantize_phase(
            "c_proj_requant",
            "c_proj_accumulator_i64",
            "c_proj_weight_scale_q8_24",
            "c_proj_bias_q16_16",
            "c_proj_output_scale_q8_24",
            "c_proj_post_weight_rescale_bias_q16_16",
            "c_proj_output_codes_i8",
            "c_proj_output_q16_16",
            256,
            64,
        ),
    ]
    phase_cells = [cell for cells, _, _ in phases for cell in cells]
    phase_wires = [wire for _, wires, _ in phases for wire in wires]
    phase_controls = [control for _, _, control in phases]
    futil = f'''// Generated exact TinyStories-1M block-0 MLP crossing.
// All mutable checkpoints are produced and handed off through memories in this main.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
{_indent_lines(memory_cells + phase_cells, 4)}
  }}
  wires {{
{_indent_lines(phase_wires, 4)}
  }}
  control {{
    seq {{
{_indent_lines(phase_controls, 0)}
    }}
  }}
}}
'''
    if len(re.findall(r"(?m)^component\s+main\b", futil)) != 1:
        raise ValueError("composed MLP generator did not emit exactly one main")
    return futil


def generate_composed_mlp_kernel(fixture_path: Path) -> CalyxArtifact:
    fixture = _checked_composed_fixture(fixture_path)
    futil = _composed_mlp_kernel_futil()
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": COMPOSED_SCHEMA,
            "fixture_receipt_sha256": fixture["receipt_sha256"],
            "tensor_fixture_receipt_sha256": fixture[
                "tensor_fixture_receipt_sha256"
            ],
            "fixture_authority": _fixture_authority(fixture),
            "schema_authority": _composed_schema_authority(fixture),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "component_count": 1,
            "host_preload_memories": list(COMPOSED_SOURCE_MEMORIES),
            "checkpoint_memories": list(COMPOSED_CHECKPOINT_MEMORIES),
            "calyx_disabled_passes": ["cell-share"],
            "composition": {
                "c_fc_to_gelu": "c_fc_output_q16_16_hardware_memory",
                "gelu_to_c_proj": "gelu_output_q16_16_hardware_memory",
                "host_intermediate": False,
            },
        },
    )


def _flatten_tensor_values(fixture: dict[str, Any], name: str) -> list[int]:
    values = fixture["tensors"][name]["values"]
    if not isinstance(values, list):
        raise ValueError(f"fixture tensor values are not an array: {name}")
    if values and isinstance(values[0], list):
        return [int(value) for row in values for value in row]
    return [int(value) for value in values]


def _cpp_print_memory(
    json_name: str, memory: str, count: int, *, signed_i8: bool = False
) -> str:
    expression = f"root->main__DOT__{memory}__DOT__mem[i]"
    if signed_i8:
        expression = (
            "static_cast<std::int64_t>(static_cast<std::int8_t>("
            + expression
            + "))"
        )
    else:
        expression = f"static_cast<std::int64_t>({expression})"
    return f'''  std::cout << ",\\\"{json_name}\\\":[";
  for (unsigned i = 0; i < {count}; ++i) {{
    if (i) std::cout << ',';
    std::cout << {expression};
  }}
  std::cout << ']';'''


def _generated_composed_mlp_harness(fixture: dict[str, Any]) -> str:
    """Preload immutable sources, zero outputs, and observe only hardware state."""
    source_arrays = (
        ("CFcInputCodes", "c_fc_input_codes_i8"),
        ("CFcInputScale", "c_fc_input_scale_q8_24"),
        ("CFcWeights", "c_fc_weight_codes_i8"),
        ("CFcWeightScale", "c_fc_weight_scale_q8_24"),
        ("CFcBias", "c_fc_bias_q16_16"),
        ("CFcOutputScale", "c_fc_output_scale_q8_24"),
        ("GeluLut", "gelu_lut_q12"),
        ("CProjInputScale", "c_proj_input_scale_q8_24"),
        ("CProjWeights", "c_proj_weight_codes_i8"),
        ("CProjWeightScale", "c_proj_weight_scale_q8_24"),
        ("CProjBias", "c_proj_bias_q16_16"),
        ("CProjOutputScale", "c_proj_output_scale_q8_24"),
    )
    declarations = []
    preload_groups = []
    cpp_names: list[str] = []
    for cpp_suffix, tensor_name in source_arrays:
        values = _flatten_tensor_values(fixture, tensor_name)
        cpp_name = f"k{cpp_suffix}"
        cpp_names.append(cpp_name)
        declarations.append(
            f"static const std::int64_t {cpp_name}[{len(values)}] = "
            f"{{{_cpp_values(values)}}};"
        )
        cast = "std::uint8_t" if tensor_name.endswith("codes_i8") else "std::uint64_t"
        preload_groups.append(
            f"  for (unsigned i = 0; i < {len(values)}; ++i)\n"
            f"    root->main__DOT__{tensor_name}__DOT__mem[i] = "
            f"static_cast<{cast}>({cpp_name}[i]);"
        )
    checkpoint_counts = {
        name: fixture["tensors"][name]["bytes"] // 8
        for name in COMPOSED_CHECKPOINT_MEMORIES
    }
    zero_groups = [
        f"  for (unsigned i = 0; i < {count}; ++i)\n"
        f"    root->main__DOT__{name}__DOT__mem[i] = 0;"
        for name, count in checkpoint_counts.items()
    ]
    print_groups = [
        _cpp_print_memory(
            name,
            name,
            count,
            signed_i8=name.endswith("codes_i8"),
        )
        for name, count in checkpoint_counts.items()
    ]
    harness = f'''// Generated direct-memory MLP crossing harness.
// Static arrays below are exactly the authenticated immutable source memories.
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
  const bool complete = model.done && cycles > 131072;
  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "timeout")
            << "\\\",\\\"cycles\\\":" << cycles;
{chr(10).join(print_groups)}
  std::cout << "}}\\n";
  return complete ? 0 : 1;
}}
'''
    found_arrays = re.findall(
        r"^static const std::int64_t\s+(k[A-Za-z0-9_]+)\[",
        harness,
        re.MULTILINE,
    )
    if found_arrays != cpp_names:
        raise ValueError("composed MLP harness source preload whitelist mismatch")
    for name in COMPOSED_CHECKPOINT_MEMORIES:
        assignment = rf"main__DOT__{re.escape(name)}__DOT__mem\[i\]\s*=\s*([^;]+);"
        assigned = re.findall(assignment, harness)
        if assigned != ["0"]:
            raise ValueError(f"host writes composed checkpoint memory: {name}")
    return harness


def _artifact_record(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _require_exact_checkpoint(
    name: str, observed: list[int], fixture: dict[str, Any]
) -> dict[str, object]:
    expected = _flatten_tensor_values(fixture, name)
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
    expected_digest = fixture["tensors"][name]["little_endian_int64_sha256"]
    if digest != expected_digest:
        raise RuntimeError(f"generated-SV composed {name} hash mismatch")
    return {"count": len(observed), "little_endian_int64_sha256": digest}


def run_composed_mlp_sv(fixture_path: Path) -> dict[str, object]:
    """Compile and observe the complete direct-memory MLP crossing once."""
    artifact = generate_composed_mlp_kernel(fixture_path)
    fixture = _checked_composed_fixture(fixture_path)
    if (
        artifact.provenance.get("fixture_receipt_sha256")
        != fixture["receipt_sha256"]
        or artifact.provenance.get("tensor_fixture_receipt_sha256")
        != fixture["tensor_fixture_receipt_sha256"]
        or artifact.provenance.get("fixture_authority")
        != _fixture_authority(fixture)
        or artifact.provenance.get("schema_authority")
        != _composed_schema_authority(fixture)
    ):
        raise ValueError("composed generated-SV fixture/schema authority mismatch")
    if artifact.provenance.get("futil_sha256") != hashlib.sha256(
        artifact.futil.encode("utf-8")
    ).hexdigest():
        raise ValueError("composed generated-SV Futil authority mismatch")

    artifact_dir = COMPOSED_ARTIFACT_DIRECTORY
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "mlp-crossing.futil"
    sv_path = artifact_dir / "main.sv"
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(_generated_composed_mlp_harness(fixture), encoding="utf-8")

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
            f"composed MLP Calyx-to-SV failed: {simulator_compile.stderr.strip()}"
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
            "composed MLP Calyx synthesis-to-SV failed: "
            f"{synthesis_compile.stderr.strip()}"
        )

    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_mlp_crossing_harness"
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
        timeout=600,
    )
    if verilator_compile.returncode != 0 or not executable.is_file():
        raise RuntimeError(
            f"composed MLP Verilator build failed: {verilator_compile.stderr.strip()}"
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
            "generated-SV composed MLP harness failed: "
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
            "generated-SV composed MLP harness did not emit JSON: "
            f"{simulated.stdout!r}"
        ) from error
    if observed.get("status") != "ok" or not isinstance(observed.get("cycles"), int):
        raise RuntimeError(f"generated-SV composed MLP observation is invalid: {observed}")

    observed_receipts: dict[str, object] = {}
    for name in COMPOSED_CHECKPOINT_MEMORIES:
        values = observed.get(name)
        if not isinstance(values, list) or not all(
            isinstance(value, int) for value in values
        ):
            raise RuntimeError(f"generated-SV composed MLP checkpoint invalid: {name}")
        observed_receipts[name] = _require_exact_checkpoint(name, values, fixture)

    generated_artifacts = {
        "futil": _artifact_record(futil_path),
        "sv": _artifact_record(sv_path),
        "synthesis_sv": _artifact_record(synthesis_sv_path),
        "harness": _artifact_record(harness_path),
    }
    receipt: dict[str, object] = {
        "schema": COMPOSED_SCHEMA,
        "fixture_receipt_sha256": fixture["receipt_sha256"],
        "tensor_fixture_receipt_sha256": fixture[
            "tensor_fixture_receipt_sha256"
        ],
        "fixture_authority": _fixture_authority(fixture),
        "schema_authority": _composed_schema_authority(fixture),
        "execution": {
            "component_count": 1,
            "simulator_runs": 1,
            "host_intermediate": False,
            "cycles": observed["cycles"],
            "traversal": "c_fc_input_qdq_then_row_output_ascending_k_then_gelu_then_c_proj_input_qdq_then_row_output_ascending_k",
            "c_fc_to_gelu_handoff": "c_fc_output_q16_16_hardware_memory",
            "gelu_to_c_proj_handoff": "gelu_output_q16_16_hardware_memory",
            "host_preload_memories": list(COMPOSED_SOURCE_MEMORIES),
        },
        "observed": observed_receipts,
        "generated_artifacts": generated_artifacts,
        "calyx_compile_policy": {
            "disabled_passes": ["cell-share"],
            "reason": "preserve explicitly staged fixed-point arithmetic latches",
            "simulator_command": simulator_command,
            "synthesis_command": synthesis_command,
            "same_futil_sha256": generated_artifacts["futil"]["sha256"],
        },
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _canonical_sha256(unsigned):
        raise RuntimeError("generated-SV composed MLP receipt self-hash mismatch")
    return receipt
