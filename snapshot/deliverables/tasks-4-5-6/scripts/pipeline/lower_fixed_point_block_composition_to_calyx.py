#!/usr/bin/env python3
"""Generate and observe one exact TinyStories-1M block-0 Calyx component."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BLOCK_SCHEMA = "tinystories-1m-fixed-point-block-composition-generated-sv-v1"
BLOCK_FIXTURE_SCHEMA = "tinystories-1m-fixed-point-block-composition-slice-v1"
CELL_SHARE_EXCEPTION_REASON = (
    "disable cell-share because default sharing makes the exact complete-block "
    "non-synthesis simulation SV contain circular combinational logic at "
    "main.gelu_input_abs_out; Verilator 5.022 rejects it as UNOPTFLAT, while "
    "synthesis SV passes Yosys with only expected undriven external-memory "
    "write_data warnings"
)
ARTIFACT_DIRECTORY = Path(
    "/tmp/llm2fpga-tinystories-1m-fixed-point-block-composition-generated-sv-v1"
)


@dataclass(frozen=True)
class CalyxArtifact:
    futil: str
    provenance: dict[str, Any]


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"compiler dependency is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _attention_lowerer():
    return _load_module(
        ROOT / "scripts/pipeline/lower_fixed_point_attention_crossing_to_calyx.py",
        "fixed_point_attention_crossing_for_block_composition",
    )


def _mlp_lowerer():
    return _load_module(
        ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py",
        "fixed_point_mlp_crossing_for_block_composition",
    )


def _block_capture():
    return _load_module(
        ROOT / "TinyStories/capture_fixed_point_block_composition_slice.py",
        "fixed_point_block_composition_capture_for_calyx",
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fixture_authority(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": fixture["schema"],
        "receipt_sha256": fixture["receipt_sha256"],
        "tensor_fixture_receipt_sha256": fixture[
            "tensor_fixture_receipt_sha256"
        ],
        "identity": fixture["identity"],
        "tensors": {
            name: {
                key: record[key]
                for key in (
                    "semantic",
                    "shape",
                    "dtype",
                    "bytes",
                    "canonical_sha256",
                    "little_endian_int64_sha256",
                )
            }
            for name, record in sorted(fixture["tensors"].items())
        },
    }


def _checked_fixtures(
    block_fixture: Path, attention_fixture: Path, mlp_fixture: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Authenticate all authorities and their exact cross-fixture bindings."""
    attention_lowerer = _attention_lowerer()
    attention, mlp = attention_lowerer._checked_composed_attention_fixtures(
        attention_fixture, mlp_fixture
    )
    block_capture = _block_capture()
    block = block_capture.verify_fixture(block_fixture)
    block_capture.verify_block_output_replay(block_fixture)
    if block.get("schema") != BLOCK_FIXTURE_SCHEMA:
        raise ValueError("block-composition fixture schema authority mismatch")
    links = (
        ("attention", block.get("linked_attention"), attention),
        ("mlp", block.get("linked_mlp"), mlp),
    )
    for label, linked, fixture in links:
        if not isinstance(linked, dict):
            raise ValueError(f"block fixture lacks linked {label} authority")
        for key in (
            "schema",
            "receipt_sha256",
            "tensor_fixture_receipt_sha256",
            "identity",
        ):
            if linked.get(key) != fixture.get(key):
                raise ValueError(f"block fixture linked {label} authority mismatch")
        receipts = linked.get("tensor_receipts")
        if not isinstance(receipts, dict) or set(receipts) != set(fixture["tensors"]):
            raise ValueError(f"block fixture linked {label} record set mismatch")
        for name, record in fixture["tensors"].items():
            for key, value in receipts[name].items():
                if record.get(key) != value:
                    raise ValueError(
                        f"block fixture linked {label} record mismatch: {name}"
                    )
    for name in set(attention["tensors"]) & set(mlp["tensors"]):
        shared_fields = (
            "semantic",
            "shape",
            "dtype",
            "values",
            "canonical_sha256",
            "little_endian_int64_sha256",
            "bytes",
        )
        if any(
            attention["tensors"][name].get(field)
            != mlp["tensors"][name].get(field)
            for field in shared_fields
        ):
            raise ValueError(f"attention/MLP shared checkpoint mismatch: {name}")
    return block, attention, mlp


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _memory_contracts(attention_lowerer, mlp_lowerer):
    sources = tuple(
        sorted(
            set(attention_lowerer.COMPOSED_ATTENTION_SOURCE_MEMORIES)
            | (
                set(mlp_lowerer.COMPOSED_SOURCE_MEMORIES)
                - {"c_fc_input_codes_i8"}
            )
        )
    )
    computed = _unique(
        [
            *attention_lowerer.COMPOSED_ATTENTION_COMPUTED_CHECKPOINTS,
            *(
                name
                for name in mlp_lowerer.COMPOSED_CHECKPOINT_MEMORIES
                if name != "c_fc_input_q16_16"
            ),
            "block_output_q16_16",
        ]
    )
    logical = _unique(
        [
            *attention_lowerer.COMPOSED_ATTENTION_OBSERVED_TENSORS,
            *mlp_lowerer.COMPOSED_SOURCE_MEMORIES,
            *mlp_lowerer.COMPOSED_CHECKPOINT_MEMORIES,
            "gelu_input_q16_16",
            "block_output_q16_16",
        ]
    )
    return sources, computed, logical


def _final_residual_phase() -> tuple[list[str], list[str], str]:
    cells = [
        "block_output_counter = std_reg(9);",
        "block_output_lt = std_lt(9);",
        "block_output_increment = std_add(9);",
        "block_output_address = std_slice(9, 8);",
        "block_output_add = std_sadd(64);",
    ]
    wires = ["""block_output_address.in = block_output_counter.out;
    group block_output_init_counter {
      block_output_counter.in = 9'd0;
      block_output_counter.write_en = 1'd1;
      block_output_init_counter[done] = block_output_counter.done;
    }
    group block_output_read {
      attention_residual_q16_16.addr0 = block_output_address.out;
      attention_residual_q16_16.content_en = 1'd1;
      c_proj_output_q16_16.addr0 = block_output_address.out;
      c_proj_output_q16_16.content_en = 1'd1;
      block_output_read[done] = (attention_residual_q16_16.done & c_proj_output_q16_16.done) ? 1'd1;
    }
    group block_output_write {
      block_output_add.left = attention_residual_q16_16.read_data;
      block_output_add.right = c_proj_output_q16_16.read_data;
      block_output_q16_16.addr0 = block_output_address.out;
      block_output_q16_16.content_en = 1'd1;
      block_output_q16_16.write_data = block_output_add.out;
      block_output_q16_16.write_en = 1'd1;
      block_output_write[done] = block_output_q16_16.done;
    }
    group block_output_increment_counter {
      block_output_increment.left = block_output_counter.out;
      block_output_increment.right = 9'd1;
      block_output_counter.in = block_output_increment.out;
      block_output_counter.write_en = 1'd1;
      block_output_increment_counter[done] = block_output_counter.done;
    }
    comb group block_output_condition {
      block_output_lt.left = block_output_counter.out;
      block_output_lt.right = 9'd256;
    }"""]
    control = """      block_output_init_counter;
      while block_output_lt.out with block_output_condition {
        seq { block_output_read; block_output_write; block_output_increment_counter; }
      }"""
    return cells, wires, control


def _block_kernel_futil() -> str:
    """Append proven generated MLP phases and final residual to attention."""
    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    attention_futil = attention_lowerer._composed_attention_kernel_futil()
    attention_cells, attention_wires, attention_control = (
        mlp_lowerer._component_sections(attention_futil)
    )
    additional_memories = [
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
        "@external block_output_q16_16 = seq_mem_d1(64, 256, 8);",
    ]
    phases = [
        mlp_lowerer._gemv_phase(
            "c_fc_gemv",
            "c_fc_input_codes_i8",
            "c_fc_input_scale_q8_24",
            "c_fc_weight_codes_i8",
            "c_fc_accumulator_i64",
            4,
            64,
            256,
        ),
        mlp_lowerer._requantize_phase(
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
        mlp_lowerer._gelu_phase(),
        mlp_lowerer._activation_qdq_phase(
            "c_proj_input_qdq",
            "gelu_output_q16_16",
            "c_proj_input_scale_q8_24",
            "c_proj_input_codes_i8",
            "c_proj_input_q16_16",
            1024,
            256,
        ),
        mlp_lowerer._gemv_phase(
            "c_proj_gemv",
            "c_proj_input_codes_i8",
            "c_proj_input_scale_q8_24",
            "c_proj_weight_codes_i8",
            "c_proj_accumulator_i64",
            4,
            256,
            64,
        ),
        mlp_lowerer._requantize_phase(
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
        _final_residual_phase(),
    ]
    cells = [
        *(line.strip() for line in attention_cells.splitlines() if line.strip()),
        *additional_memories,
        *(cell for phase_cells, _, _ in phases for cell in phase_cells),
    ]
    wires = [wire for _, phase_wires, _ in phases for wire in phase_wires]
    controls = [control for _, _, control in phases]
    futil = f'''// Generated exact TinyStories-1M block-0 composition.
// One component owns attention, MLP, both crossings, and the final residual.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go go: 1) -> (@done done: 1) {{
  cells {{
{mlp_lowerer._indent_lines(cells, 4)}
  }}
  wires {{
{attention_wires.rstrip()}
{mlp_lowerer._indent_lines(wires, 4)}
  }}
  control {{
    seq {{
{attention_control.rstrip()}
{mlp_lowerer._indent_lines(controls, 0)}
    }}
  }}
}}
'''
    if len(re.findall(r"(?m)^component\s+main\b", futil)) != 1:
        raise ValueError("block generator did not emit exactly one main")
    cell_section, wire_section, _ = mlp_lowerer._component_sections(futil)
    cell_names = re.findall(
        r"(?m)^\s*(?:@external\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", cell_section
    )
    group_names = re.findall(
        r"(?m)^\s*(?:comb\s+)?group\s+([A-Za-z_][A-Za-z0-9_]*)", wire_section
    )
    duplicates = sorted(
        {name for name in (*cell_names, *group_names) if (*cell_names, *group_names).count(name) > 1}
    )
    if duplicates:
        raise ValueError(f"block generator name collision: {duplicates[0]}")
    required_reads = (
        "c_fc_input_qdq_numerator_lshift.left = ln2_output_q16_16.read_data;",
        "c_fc_gemv_code_signed.in = c_fc_input_codes_i8.read_data;",
        "block_output_add.left = attention_residual_q16_16.read_data;",
        "block_output_add.right = c_proj_output_q16_16.read_data;",
    )
    for statement in required_reads:
        if statement not in futil:
            raise ValueError(f"missing direct generated-memory read: {statement}")
    _, computed, _ = _memory_contracts(attention_lowerer, mlp_lowerer)
    for name in computed:
        if f"{name}.write_en = 1'd1" not in futil:
            raise ValueError(f"block checkpoint is not hardware-written: {name}")
    return futil


def _schema_authority(
    block: dict[str, Any], attention: dict[str, Any], mlp: dict[str, Any]
) -> dict[str, Any]:
    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    sources, computed, logical = _memory_contracts(attention_lowerer, mlp_lowerer)
    payload: dict[str, Any] = {
        "schema": BLOCK_SCHEMA,
        "block_fixture_schema": block["schema"],
        "attention_fixture_schema": attention["schema"],
        "mlp_fixture_schema": mlp["schema"],
        "layer": 0,
        "rows": 4,
        "width": 64,
        "source_memories": list(sources),
        "hardware_owned_memories": list(computed),
        "observed_records": list(logical),
        "handoffs": [
            "ln2_output_q16_16_to_c_fc_input_qdq",
            "attention_residual_q16_16_and_c_proj_output_q16_16_to_block_output_q16_16",
        ],
    }
    payload["receipt_sha256"] = _canonical_sha256(payload)
    return payload


def generate_block_kernel(
    block_fixture: Path, attention_fixture: Path, mlp_fixture: Path
) -> CalyxArtifact:
    block, attention, mlp = _checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    sources, computed, logical = _memory_contracts(attention_lowerer, mlp_lowerer)
    futil = _block_kernel_futil()
    attention_generator = (
        ROOT / "scripts/pipeline/lower_fixed_point_attention_crossing_to_calyx.py"
    )
    mlp_generator = (
        ROOT / "scripts/pipeline/lower_fixed_point_mlp_crossing_to_calyx.py"
    )
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": BLOCK_SCHEMA,
            "block_fixture_authority": _fixture_authority(block),
            "attention_fixture_authority": _fixture_authority(attention),
            "mlp_fixture_authority": _fixture_authority(mlp),
            "schema_authority": _schema_authority(block, attention, mlp),
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "attention_generator_sha256": hashlib.sha256(
                attention_generator.read_bytes()
            ).hexdigest(),
            "mlp_generator_sha256": hashlib.sha256(
                mlp_generator.read_bytes()
            ).hexdigest(),
            "component_count": 1,
            "host_preload_memories": list(sources),
            "hardware_owned_memories": list(computed),
            "observed_records": list(logical),
            "calyx_disabled_passes": ["cell-share"],
            "host_intermediate": False,
        },
    )


def _flatten(value: object) -> list[int]:
    if not isinstance(value, list):
        return [int(value)]
    result: list[int] = []
    for item in value:
        result.extend(_flatten(item))
    return result


def _record_for(
    name: str,
    block: dict[str, Any],
    attention: dict[str, Any],
    mlp: dict[str, Any],
) -> dict[str, Any]:
    records = [
        fixture["tensors"][name]
        for fixture in (attention, mlp, block)
        if name in fixture["tensors"]
    ]
    if not records:
        raise ValueError(f"no fixture authority for memory: {name}")
    shared_fields = (
        "semantic",
        "shape",
        "dtype",
        "values",
        "canonical_sha256",
        "little_endian_int64_sha256",
        "bytes",
    )
    if any(
        any(record.get(field) != records[0].get(field) for field in shared_fields)
        for record in records[1:]
    ):
        raise ValueError(f"conflicting fixture authorities for memory: {name}")
    return records[0]


def _physical_memory(name: str) -> str:
    if name == "gelu_input_q16_16":
        return "c_fc_output_q16_16"
    return name


def _cpp_values(values: list[int]) -> str:
    return ",".join(str(value) for value in values)


def _cpp_print_memory(
    json_name: str, memory: str, count: int, *, signed_i8: bool
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


def _generated_harness(
    block: dict[str, Any], attention: dict[str, Any], mlp: dict[str, Any]
) -> str:
    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    sources, computed, logical = _memory_contracts(attention_lowerer, mlp_lowerer)
    declarations: list[str] = []
    preloads: list[str] = []
    cpp_names: list[str] = []
    for index, name in enumerate(sources):
        values = _flatten(_record_for(name, block, attention, mlp)["values"])
        cpp_name = f"kSource{index:02d}"
        cpp_names.append(cpp_name)
        declarations.append(
            f"// {name}\nstatic const std::int64_t {cpp_name}[{len(values)}] = "
            f"{{{_cpp_values(values)}}};"
        )
        cast = "std::uint8_t" if name.endswith("codes_i8") else "std::uint64_t"
        preloads.append(
            f"  for (unsigned i = 0; i < {len(values)}; ++i)\n"
            f"    root->main__DOT__{name}__DOT__mem[i] = "
            f"static_cast<{cast}>({cpp_name}[i]);"
        )
    zeroes = []
    for name in computed:
        record = _record_for(name, block, attention, mlp)
        count = len(_flatten(record["values"]))
        zeroes.append(
            f"  for (unsigned i = 0; i < {count}; ++i)\n"
            f"    root->main__DOT__{name}__DOT__mem[i] = 0;"
        )
    prints = []
    for name in logical:
        record = _record_for(name, block, attention, mlp)
        prints.append(
            _cpp_print_memory(
                name,
                _physical_memory(name),
                len(_flatten(record["values"])),
                signed_i8=name.endswith("codes_i8"),
            )
        )
    harness = f'''// Generated source-only exact complete-block harness.
// Computed memories receive zero initialization, never expected values.
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
{chr(10).join(preloads)}
{chr(10).join(zeroes)}
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
{chr(10).join(prints)}
  std::cout << "}}\\n";
  return complete ? 0 : 1;
}}
'''
    found = re.findall(
        r"^static const std::int64_t\s+(kSource[0-9]+)\[", harness, re.MULTILINE
    )
    if found != cpp_names or len(found) != len(sources):
        raise ValueError("complete-block source preload whitelist mismatch")
    if "kExpected" in harness:
        raise ValueError("complete-block harness contains an expected-output oracle")
    for name in computed:
        assignment = rf"main__DOT__{re.escape(name)}__DOT__mem\[i\]\s*=\s*([^;]+);"
        if re.findall(assignment, harness) != ["0"]:
            raise ValueError(f"host writes complete-block checkpoint: {name}")
    return harness


def _calyx_install() -> Path:
    return _attention_lowerer()._calyx_install()


def _reshape(values: list[int], shape: list[int]) -> object:
    if not shape:
        if len(values) != 1:
            raise ValueError("scalar observation length mismatch")
        return values[0]
    if len(shape) == 1:
        if len(values) != shape[0]:
            raise ValueError("observation shape mismatch")
        return values
    stride = 1
    for dimension in shape[1:]:
        stride *= dimension
    if len(values) != shape[0] * stride:
        raise ValueError("observation shape mismatch")
    return [
        _reshape(values[index * stride : (index + 1) * stride], shape[1:])
        for index in range(shape[0])
    ]


def _little_endian_i64_sha256(values: list[int]) -> str:
    raw = b"".join(struct.pack("<q", value) for value in values)
    return hashlib.sha256(raw).hexdigest()


def _artifact_record(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _calyx_common_command(futil_path: Path) -> list[str]:
    calyx = _calyx_install()
    return [
        str(calyx / "bin/calyx"),
        str(futil_path),
        "-l",
        str(calyx / "share/calyx"),
        "-d",
        "cell-share",
    ]


def _cell_share_exception_evidence(futil_sha256: str) -> dict[str, object]:
    return {
        "futil_sha256": futil_sha256,
        "default_cell_share_simulation_sv": {
            "calyx_mode": "-b verilog (without --synthesis)",
            "verilator": "5.022",
            "result": "rejected",
            "diagnostic_class": "UNOPTFLAT circular combinational logic",
            "first_signal": "main.gelu_input_abs_out",
        },
        "default_cell_share_synthesis_sv": {
            "calyx_mode": "--synthesis --disable-verify -b verilog",
            "yosys": "0.66",
            "post_techmap_loop_diagnostics": 0,
            "expected_undriven_external_memory_write_data_bits": 1840,
        },
    }


def _validate_yosys_stat(stdout: str) -> dict[str, int]:
    if "=== main ===" not in stdout or "=== design hierarchy ===" not in stdout:
        raise RuntimeError("Yosys stat did not report the generated main hierarchy")
    hierarchy = stdout.rsplit("=== design hierarchy ===", maxsplit=1)[1]
    resources: dict[str, int] = {}
    for key, label in (
        ("cells", "cells"),
        ("memories", "memories"),
        ("memory_bits", "memory bits"),
    ):
        match = re.search(rf"(?m)^\s*(\d+)\s+{re.escape(label)}\s*$", hierarchy)
        if match is None:
            raise RuntimeError(f"Yosys stat is missing hierarchy {label}")
        resources[key] = int(match.group(1))
    if any(value <= 0 for value in resources.values()):
        raise RuntimeError("Yosys stat reported an empty generated hierarchy")
    return resources


def _linked_fixture_receipt(
    block: dict[str, Any], attention: dict[str, Any], mlp: dict[str, Any]
) -> dict[str, object]:
    return {
        "block_receipt_sha256": block["receipt_sha256"],
        "block_tensor_fixture_receipt_sha256": block[
            "tensor_fixture_receipt_sha256"
        ],
        "attention_receipt_sha256": attention["receipt_sha256"],
        "attention_tensor_fixture_receipt_sha256": attention[
            "tensor_fixture_receipt_sha256"
        ],
        "mlp_receipt_sha256": mlp["receipt_sha256"],
        "mlp_tensor_fixture_receipt_sha256": mlp[
            "tensor_fixture_receipt_sha256"
        ],
        "block_linked_attention_receipt_sha256": block["linked_attention"][
            "receipt_sha256"
        ],
        "block_linked_mlp_receipt_sha256": block["linked_mlp"][
            "receipt_sha256"
        ],
        "mlp_c_fc_input_q16_16_sha256": mlp["tensors"][
            "c_fc_input_q16_16"
        ]["little_endian_int64_sha256"],
        "record_counts": {
            "attention": len(attention["tensors"]),
            "mlp": len(mlp["tensors"]),
            "block": len(block["tensors"]),
            "unique": len(
                set(attention["tensors"])
                | set(mlp["tensors"])
                | set(block["tensors"])
            ),
        },
        "identities": {
            "block": block["identity"],
            "attention": attention["identity"],
            "mlp": mlp["identity"],
        },
    }


def run_block_sv(
    artifact: CalyxArtifact,
    block_fixture: Path,
    attention_fixture: Path,
    mlp_fixture: Path,
) -> dict[str, object]:
    """Compile once and compare every hardware-observed complete-block record."""
    block, attention, mlp = _checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    expected_artifact = generate_block_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    if artifact.futil != expected_artifact.futil or artifact.provenance != expected_artifact.provenance:
        raise ValueError("complete-block artifact fixture/schema authority mismatch")
    if artifact.provenance["futil_sha256"] != hashlib.sha256(
        artifact.futil.encode("utf-8")
    ).hexdigest():
        raise ValueError("complete-block Futil authority mismatch")

    artifact_dir = ARTIFACT_DIRECTORY
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "block-composition.futil"
    sv_path = artifact_dir / "main.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness = _generated_harness(block, attention, mlp)
    harness_path.write_text(harness, encoding="utf-8")

    calyx_command = [
        *_calyx_common_command(futil_path),
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
            f"complete-block Calyx-to-SV failed: {compiled.stderr.strip()}"
        )

    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_block_composition_harness"
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
        timeout=900,
    )
    if verilated.returncode != 0 or not executable.is_file():
        raise RuntimeError(
            f"complete-block Verilator build failed: {verilated.stderr.strip()}"
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
            "generated-SV complete-block harness failed: "
            f"{simulated.stdout[-2000:]} {simulated.stderr.strip()}"
        )
    try:
        raw_observed = json.loads(
            next(
                line
                for line in reversed(simulated.stdout.splitlines())
                if line.startswith("{")
            )
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError("complete-block harness did not emit JSON") from error
    if raw_observed.get("status") != "ok" or not isinstance(
        raw_observed.get("cycles"), int
    ):
        raise RuntimeError("complete-block generated-SV observation is invalid")

    logical = artifact.provenance["observed_records"]
    result: dict[str, object] = {
        "component_count": 1,
        "host_intermediate": False,
        "cycles": raw_observed["cycles"],
        "little_endian_int64_sha256": {},
        "generated_sv_artifacts": {
            "futil": str(futil_path),
            "sv": str(sv_path),
            "harness": str(harness_path),
        },
    }
    hashes = result["little_endian_int64_sha256"]
    assert isinstance(hashes, dict)
    for name in logical:
        values = raw_observed.get(name)
        record = _record_for(name, block, attention, mlp)
        expected = _flatten(record["values"])
        if not isinstance(values, list) or not all(isinstance(value, int) for value in values):
            raise RuntimeError(f"generated-SV complete-block checkpoint invalid: {name}")
        if len(values) != len(expected):
            raise RuntimeError(
                f"generated-SV {name} length mismatch: {len(values)} != {len(expected)}"
            )
        for index, (actual, wanted) in enumerate(zip(values, expected, strict=True)):
            if actual != wanted:
                raise RuntimeError(
                    f"generated-SV {name} first mismatch at {index}: "
                    f"observed {actual}, expected {wanted}"
                )
        digest = _little_endian_i64_sha256(values)
        if digest != record["little_endian_int64_sha256"]:
            raise RuntimeError(f"generated-SV {name} raw hash mismatch")
        result[name] = _reshape(values, record["shape"])
        hashes[name] = digest
    return result


def _expected_checkpoint_receipts(
    block: dict[str, Any],
    attention: dict[str, Any],
    mlp: dict[str, Any],
    logical: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    return {
        name: {
            "count": _record_for(name, block, attention, mlp)["bytes"] // 8,
            "little_endian_int64_sha256": _record_for(
                name, block, attention, mlp
            )["little_endian_int64_sha256"],
        }
        for name in logical
    }


def _require_receipt_field(
    actual: object, expected: object, label: str
) -> None:
    if actual != expected:
        raise ValueError(f"complete-block receipt {label} mismatch")


def validate_composed_block_receipt(
    receipt: dict[str, object],
    block_fixture: Path,
    attention_fixture: Path,
    mlp_fixture: Path,
    *,
    verify_artifacts: bool = True,
) -> dict[str, object]:
    """Authenticate a complete-block receipt against fixtures and artifacts."""
    if not isinstance(receipt, dict):
        raise ValueError("complete-block receipt must be an object")
    supplied_self_hash = receipt.get("receipt_sha256")
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    if supplied_self_hash != _canonical_sha256(unsigned):
        raise ValueError("complete-block receipt self-hash mismatch")

    block, attention, mlp = _checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    sources, computed, logical = _memory_contracts(
        attention_lowerer, mlp_lowerer
    )
    if len(logical) != 84:
        raise ValueError("complete-block receipt expected 84 unique records")

    _require_receipt_field(receipt.get("schema"), BLOCK_SCHEMA, "schema")
    _require_receipt_field(
        receipt.get("fixture_authorities"),
        {
            "block": _fixture_authority(block),
            "attention": _fixture_authority(attention),
            "mlp": _fixture_authority(mlp),
        },
        "fixture authorities",
    )
    _require_receipt_field(
        receipt.get("linked_fixtures"),
        _linked_fixture_receipt(block, attention, mlp),
        "linked fixtures",
    )
    _require_receipt_field(
        receipt.get("schema_authority"),
        _schema_authority(block, attention, mlp),
        "schema authority",
    )

    expected_execution = {
        "component_count": 1,
        "simulator_runs": 1,
        "host_intermediate": False,
        "cycles": receipt.get("execution", {}).get("cycles")
        if isinstance(receipt.get("execution"), dict)
        else None,
        "host_preload_memories": list(sources),
        "hardware_owned_memories": list(computed),
        "observed_records": list(logical),
        "direct_handoffs": [
            "ln2_output_q16_16_to_c_fc_input_qdq",
            "attention_residual_q16_16_and_c_proj_output_q16_16_to_block_output_q16_16",
        ],
    }
    execution = receipt.get("execution")
    if not isinstance(execution, dict) or not isinstance(
        execution.get("cycles"), int
    ) or execution["cycles"] <= 131072:
        raise ValueError("complete-block receipt cycle count is invalid")
    expected_execution["cycles"] = execution["cycles"]
    _require_receipt_field(execution, expected_execution, "execution")

    expected_checkpoints = _expected_checkpoint_receipts(
        block, attention, mlp, logical
    )
    expected_observed = {
        "record_count": 84,
        "checkpoints": expected_checkpoints,
        "block_output_q16_16_sha256": expected_checkpoints[
            "block_output_q16_16"
        ]["little_endian_int64_sha256"],
    }
    _require_receipt_field(
        receipt.get("observed"), expected_observed, "observed records"
    )

    generated_artifacts = receipt.get("generated_artifacts")
    if not isinstance(generated_artifacts, dict) or set(generated_artifacts) != {
        "futil",
        "sv",
        "synthesis_sv",
        "harness",
    }:
        raise ValueError("complete-block receipt generated artifacts mismatch")
    if verify_artifacts:
        for name, record in generated_artifacts.items():
            if not isinstance(record, dict) or not isinstance(
                record.get("path"), str
            ):
                raise ValueError(f"complete-block receipt {name} artifact invalid")
            path = Path(record["path"])
            if not path.is_file() or _artifact_record(path) != record:
                raise ValueError(
                    f"complete-block receipt {name} artifact hash mismatch"
                )
        expected_futil = generate_block_kernel(
            block_fixture, attention_fixture, mlp_fixture
        ).futil.encode("utf-8")
        if generated_artifacts["futil"]["sha256"] != hashlib.sha256(
            expected_futil
        ).hexdigest():
            raise ValueError("complete-block receipt generated Futil mismatch")

    common = _calyx_common_command(
        Path(generated_artifacts["futil"]["path"])
    )
    expected_policy = {
        "disabled_passes": ["cell-share"],
        "reason": CELL_SHARE_EXCEPTION_REASON,
        "exception_evidence": _cell_share_exception_evidence(
            generated_artifacts["futil"]["sha256"]
        ),
        "simulator_command": [
            *common,
            "-b",
            "verilog",
            "-o",
            generated_artifacts["sv"]["path"],
        ],
        "synthesis_command": [
            *common,
            "--synthesis",
            "--disable-verify",
            "-b",
            "verilog",
            "-o",
            generated_artifacts["synthesis_sv"]["path"],
        ],
        "same_futil_sha256": generated_artifacts["futil"]["sha256"],
    }
    _require_receipt_field(
        receipt.get("calyx_compile_policy"), expected_policy, "compiler arguments"
    )

    synthesis = receipt.get("synthesis")
    if not isinstance(synthesis, dict):
        raise ValueError("complete-block receipt synthesis evidence is invalid")
    expected_yosys_command = [
        "yosys",
        "-p",
        "read_verilog -sv "
        f"{generated_artifacts['synthesis_sv']['path']}; "
        "hierarchy -check -top main; stat",
    ]
    if (
        synthesis.get("status") != "passed"
        or synthesis.get("same_futil_sha256")
        != generated_artifacts["futil"]["sha256"]
        or synthesis.get("synthesis_sv_sha256")
        != generated_artifacts["synthesis_sv"]["sha256"]
        or synthesis.get("command") != expected_yosys_command
        or not isinstance(synthesis.get("resources"), dict)
        or set(synthesis["resources"]) != {"cells", "memories", "memory_bits"}
        or any(
            not isinstance(value, int) or value <= 0
            for value in synthesis["resources"].values()
        )
    ):
        raise ValueError("complete-block receipt synthesis evidence mismatch")
    return receipt


def run_composed_block_sv(
    block_fixture: Path,
    attention_fixture: Path,
    mlp_fixture: Path,
) -> dict[str, object]:
    """Run the full generated-SV block gate and bind same-Futil synthesis."""
    artifact = generate_block_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    observed = run_block_sv(
        artifact, block_fixture, attention_fixture, mlp_fixture
    )
    block, attention, mlp = _checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    expected_artifact = generate_block_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    if artifact != expected_artifact:
        raise ValueError("complete-block authority changed during execution")

    artifact_dir = ARTIFACT_DIRECTORY
    futil_path = artifact_dir / "block-composition.futil"
    sv_path = artifact_dir / "main.sv"
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    harness_path = artifact_dir / "harness.cpp"
    if futil_path.read_text(encoding="utf-8") != artifact.futil:
        raise RuntimeError("complete-block written Futil changed after simulation")

    common = _calyx_common_command(futil_path)
    simulator_command = [
        *common,
        "-b",
        "verilog",
        "-o",
        str(sv_path),
    ]
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
            "complete-block Calyx synthesis-to-SV failed: "
            f"{synthesis_compile.stderr.strip()}"
        )

    generated_artifacts = {
        "futil": _artifact_record(futil_path),
        "sv": _artifact_record(sv_path),
        "synthesis_sv": _artifact_record(synthesis_sv_path),
        "harness": _artifact_record(harness_path),
    }
    if generated_artifacts["futil"]["sha256"] != artifact.provenance[
        "futil_sha256"
    ]:
        raise RuntimeError("complete-block same-Futil hash mismatch")

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
        raise RuntimeError(f"complete-block Yosys stat failed: {yosys.stderr.strip()}")
    synthesis_resources = _validate_yosys_stat(yosys.stdout)

    attention_lowerer = _attention_lowerer()
    mlp_lowerer = _mlp_lowerer()
    sources, computed, logical = _memory_contracts(
        attention_lowerer, mlp_lowerer
    )
    if len(logical) != 84 or set(logical) != set(
        observed["little_endian_int64_sha256"]
    ):
        raise RuntimeError("complete-block observation record set mismatch")
    checkpoint_receipts = {
        name: {
            "count": _record_for(name, block, attention, mlp)["bytes"] // 8,
            "little_endian_int64_sha256": observed[
                "little_endian_int64_sha256"
            ][name],
        }
        for name in logical
    }
    receipt: dict[str, object] = {
        "schema": BLOCK_SCHEMA,
        "fixture_authorities": {
            "block": _fixture_authority(block),
            "attention": _fixture_authority(attention),
            "mlp": _fixture_authority(mlp),
        },
        "linked_fixtures": _linked_fixture_receipt(block, attention, mlp),
        "schema_authority": _schema_authority(block, attention, mlp),
        "execution": {
            "component_count": 1,
            "simulator_runs": 1,
            "host_intermediate": False,
            "cycles": observed["cycles"],
            "host_preload_memories": list(sources),
            "hardware_owned_memories": list(computed),
            "observed_records": list(logical),
            "direct_handoffs": [
                "ln2_output_q16_16_to_c_fc_input_qdq",
                "attention_residual_q16_16_and_c_proj_output_q16_16_to_block_output_q16_16",
            ],
        },
        "observed": {
            "record_count": len(checkpoint_receipts),
            "checkpoints": checkpoint_receipts,
            "block_output_q16_16_sha256": checkpoint_receipts[
                "block_output_q16_16"
            ]["little_endian_int64_sha256"],
        },
        "generated_artifacts": generated_artifacts,
        "calyx_compile_policy": {
            "disabled_passes": ["cell-share"],
            "reason": CELL_SHARE_EXCEPTION_REASON,
            "exception_evidence": _cell_share_exception_evidence(
                generated_artifacts["futil"]["sha256"]
            ),
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
    return validate_composed_block_receipt(
        receipt,
        block_fixture,
        attention_fixture,
        mlp_fixture,
    )
