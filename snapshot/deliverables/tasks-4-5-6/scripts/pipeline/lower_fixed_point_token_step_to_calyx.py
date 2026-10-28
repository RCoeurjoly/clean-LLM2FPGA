#!/usr/bin/env python3
"""Generate and verify a stateful two-transaction TinyStories block-0 wrapper."""
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

import torch


ROOT = Path(__file__).resolve().parents[2]
TOKEN_STEP_SCHEMA = "tinystories-1m-fixed-point-token-step-generated-sv-v1"
ARTIFACT_DIRECTORY = Path(
    "/tmp/llm2fpga-tinystories-1m-fixed-point-token-step-generated-sv-v1"
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


def _block_lowerer():
    return _load_module(
        ROOT / "scripts/pipeline/lower_fixed_point_block_composition_to_calyx.py",
        "fixed_point_block_composition_for_token_step",
    )


def _attention_capture():
    return _load_module(
        ROOT / "TinyStories/capture_fixed_point_attention_crossing_slice.py",
        "fixed_point_attention_capture_for_token_step",
    )


def _mlp_capture():
    return _load_module(
        ROOT / "TinyStories/capture_fixed_point_mlp_crossing_slice.py",
        "fixed_point_mlp_capture_for_token_step",
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _flatten(value: object) -> list[int]:
    if not isinstance(value, list):
        return [int(value)]
    result: list[int] = []
    for item in value:
        result.extend(_flatten(item))
    return result


def _little_endian_i64_sha256(values: list[int]) -> str:
    raw = b"".join(struct.pack("<q", value) for value in values)
    return hashlib.sha256(raw).hexdigest()


def _tensor(fixture: dict[str, Any], name: str) -> torch.Tensor:
    return torch.tensor(fixture["tensors"][name]["values"], dtype=torch.int64)


def _projection(
    capture,
    fixture: dict[str, Any],
    prefix: str,
    source: torch.Tensor,
    bias: torch.Tensor | None,
) -> torch.Tensor:
    scale = _tensor(fixture, f"{prefix}_input_scale_q8_24")
    codes, _ = capture.activation_qdq(source, scale)
    accumulator = capture.serial_gemv(
        codes * scale, _tensor(fixture, f"{prefix}_weight_codes_i8")
    )
    post = capture._post_weight_rescale(
        accumulator, _tensor(fixture, f"{prefix}_weight_scale_q8_24"), bias
    )
    _, output = capture.activation_qdq(
        post, _tensor(fixture, f"{prefix}_output_scale_q8_24")
    )
    return output


def _mlp_projection(
    capture,
    fixture: dict[str, Any],
    prefix: str,
    source: torch.Tensor,
) -> torch.Tensor:
    scale = _tensor(fixture, f"{prefix}_input_scale_q8_24")
    codes, _ = capture.activation_qdq(source, scale)
    accumulator = capture.serial_gemv(
        codes * scale, _tensor(fixture, f"{prefix}_weight_codes_i8")
    )
    post = capture._post_weight_rescale_bias(
        accumulator,
        _tensor(fixture, f"{prefix}_weight_scale_q8_24"),
        _tensor(fixture, f"{prefix}_bias_q16_16"),
    )
    _, output = capture.activation_qdq(
        post, _tensor(fixture, f"{prefix}_output_scale_q8_24")
    )
    return output


def _replay_block(
    state: list[int], attention: dict[str, Any], mlp: dict[str, Any]
) -> list[int]:
    """Independently replay one exact block from its fixed-point authorities."""
    attention_capture = _attention_capture()
    mlp_capture = _mlp_capture()
    block_input = torch.tensor(state, dtype=torch.int64).reshape(4, 64)
    ln1 = attention_capture._fixed_layer_norm(
        block_input,
        _tensor(attention, "ln1_gamma_q16_16"),
        _tensor(attention, "ln1_beta_q16_16"),
    )
    query = _projection(attention_capture, attention, "q", ln1, None)
    key = _projection(attention_capture, attention, "k", ln1, None)
    value = _projection(attention_capture, attention, "v", ln1, None)
    trace = attention_capture._attention_trace(
        query, key, value, _tensor(attention, "attention_exp_lut_q1_20")
    )
    projected_attention = _projection(
        attention_capture,
        attention,
        "out",
        trace["attention_context_q16_16"],
        _tensor(attention, "out_bias_q16_16"),
    )
    attention_residual = block_input + projected_attention
    ln2 = attention_capture._fixed_layer_norm(
        attention_residual,
        _tensor(attention, "ln2_gamma_q16_16"),
        _tensor(attention, "ln2_beta_q16_16"),
    )
    c_fc = _mlp_projection(mlp_capture, mlp, "c_fc", ln2)
    gelu = mlp_capture._fixed_gelu(c_fc, _tensor(mlp, "gelu_lut_q12"))
    c_proj = _mlp_projection(mlp_capture, mlp, "c_proj", gelu)
    return (attention_residual + c_proj).reshape(-1).tolist()


def _state_wrapper_sections() -> tuple[list[str], list[str], str]:
    cells = [
        "@external input_state_q16_16 = seq_mem_d1(64, 256, 8);",
        "@external output_state_q16_16 = seq_mem_d1(64, 256, 8);",
        "token_state_counter = std_reg(9);",
        "token_state_lt = std_lt(9);",
        "token_state_increment = std_add(9);",
        "token_state_address = std_slice(9, 8);",
        "token_transaction_count = std_reg(2);",
        "token_transaction_is_first = std_eq(2);",
        "token_transaction_increment = std_add(2);",
        "token_valid_not = std_not(1);",
        "token_wait_register = std_reg(1);",
    ]
    wires = ["""comb group token_wait_for_valid_condition {
      token_valid_not.in = valid;
    }
    group token_wait_cycle {
      token_wait_register.in = 1'd1;
      token_wait_register.write_en = 1'd1;
      token_wait_cycle[done] = token_wait_register.done;
    }
    comb group token_transaction_first_condition {
      token_transaction_is_first.left = token_transaction_count.out;
      token_transaction_is_first.right = 2'd0;
    }
    comb group token_state_condition {
      token_state_lt.left = token_state_counter.out;
      token_state_lt.right = 9'd256;
    }
    group token_state_init_counter {
      token_state_counter.in = 9'd0;
      token_state_counter.write_en = 1'd1;
      token_state_init_counter[done] = token_state_counter.done;
    }
    group token_state_read_input {
      token_state_address.in = token_state_counter.out;
      input_state_q16_16.addr0 = token_state_address.out;
      input_state_q16_16.content_en = 1'd1;
      token_state_read_input[done] = input_state_q16_16.done;
    }
    group token_state_read_feedback {
      token_state_address.in = token_state_counter.out;
      output_state_q16_16.addr0 = token_state_address.out;
      output_state_q16_16.content_en = 1'd1;
      token_state_read_feedback[done] = output_state_q16_16.done;
    }
    group token_state_write_input_to_block {
      token_state_address.in = token_state_counter.out;
      block_input_q16_16.addr0 = token_state_address.out;
      block_input_q16_16.content_en = 1'd1;
      block_input_q16_16.write_data = input_state_q16_16.read_data;
      block_input_q16_16.write_en = 1'd1;
      token_state_write_input_to_block[done] = block_input_q16_16.done;
    }
    group token_state_write_feedback_to_block {
      token_state_address.in = token_state_counter.out;
      block_input_q16_16.addr0 = token_state_address.out;
      block_input_q16_16.content_en = 1'd1;
      block_input_q16_16.write_data = output_state_q16_16.read_data;
      block_input_q16_16.write_en = 1'd1;
      token_state_write_feedback_to_block[done] = block_input_q16_16.done;
    }
    group token_state_read_block_output {
      token_state_address.in = token_state_counter.out;
      block_output_q16_16.addr0 = token_state_address.out;
      block_output_q16_16.content_en = 1'd1;
      token_state_read_block_output[done] = block_output_q16_16.done;
    }
    group token_state_commit_output {
      token_state_address.in = token_state_counter.out;
      output_state_q16_16.addr0 = token_state_address.out;
      output_state_q16_16.content_en = 1'd1;
      output_state_q16_16.write_data = block_output_q16_16.read_data;
      output_state_q16_16.write_en = 1'd1;
      token_state_commit_output[done] = output_state_q16_16.done;
    }
    group token_state_increment_counter {
      token_state_increment.left = token_state_counter.out;
      token_state_increment.right = 9'd1;
      token_state_counter.in = token_state_increment.out;
      token_state_counter.write_en = 1'd1;
      token_state_increment_counter[done] = token_state_counter.done;
    }
    group token_transaction_commit {
      token_transaction_increment.left = token_transaction_count.out;
      token_transaction_increment.right = 2'd1;
      token_transaction_count.in = token_transaction_increment.out;
      token_transaction_count.write_en = 1'd1;
      token_transaction_commit[done] = token_transaction_count.done;
    }"""]
    capture = """      while token_valid_not.out with token_wait_for_valid_condition {
        token_wait_cycle;
      }
      token_state_init_counter;
      if token_transaction_is_first.out with token_transaction_first_condition {
        while token_state_lt.out with token_state_condition {
          seq {
            token_state_read_input;
            token_state_write_input_to_block;
            token_state_increment_counter;
          }
        }
      } else {
        while token_state_lt.out with token_state_condition {
          seq {
            token_state_read_feedback;
            token_state_write_feedback_to_block;
            token_state_increment_counter;
          }
        }
      }"""
    commit = """      token_state_init_counter;
      while token_state_lt.out with token_state_condition {
        seq {
          token_state_read_block_output;
          token_state_commit_output;
          token_state_increment_counter;
        }
      }
      token_transaction_commit;"""
    return cells, wires, capture + "\n{BLOCK_CONTROL}\n" + commit


def _token_step_futil(block_futil: str) -> str:
    block_lowerer = _block_lowerer()
    mlp_lowerer = block_lowerer._mlp_lowerer()
    cells, wires, control = mlp_lowerer._component_sections(block_futil)
    stripped_control = control.strip()
    match = re.fullmatch(r"seq\s*\{\n(?P<body>.*)\n\s*\}", stripped_control, re.DOTALL)
    if match is None:
        raise ValueError("complete-block control is not one composable sequence")
    block_control = match.group("body")
    wrapper_cells, wrapper_wires, wrapper_control = _state_wrapper_sections()
    full_control = wrapper_control.replace("{BLOCK_CONTROL}", block_control)
    futil = f'''// Generated stateful TinyStories-1M block-0 token-step wrapper.
// The complete-block datapath is unchanged; only state capture/commit surrounds it.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";

component main(@go start: 1, valid: 1) -> (@done done: 1) {{
  cells {{
{cells.rstrip()}
{mlp_lowerer._indent_lines(wrapper_cells, 4)}
  }}
  wires {{
{wires.rstrip()}
{mlp_lowerer._indent_lines(wrapper_wires, 4)}
  }}
  control {{
    seq {{
{full_control}
    }}
  }}
}}
'''
    if futil.count("component main(") != 1:
        raise ValueError("token-step generator did not emit exactly one main")
    for statement in (
        "component main(@go start: 1, valid: 1) -> (@done done: 1)",
        "block_input_q16_16.write_data = input_state_q16_16.read_data;",
        "block_input_q16_16.write_data = output_state_q16_16.read_data;",
        "output_state_q16_16.write_data = block_output_q16_16.read_data;",
        "while token_valid_not.out with token_wait_for_valid_condition",
    ):
        if statement not in futil:
            raise ValueError(f"missing token-step stateful contract: {statement}")
    return futil


def generate_token_step_kernel(
    block_fixture: Path, attention_fixture: Path, mlp_fixture: Path
) -> CalyxArtifact:
    block_lowerer = _block_lowerer()
    block_artifact = block_lowerer.generate_block_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    block, attention, mlp = block_lowerer._checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    first_input = _flatten(attention["tensors"]["block_input_q16_16"]["values"])
    first_output = _replay_block(first_input, attention, mlp)
    expected_fixture_output = _flatten(
        block["tensors"]["block_output_q16_16"]["values"]
    )
    if first_output != expected_fixture_output:
        raise ValueError("token-step oracle does not reproduce block authority")
    second_output = _replay_block(first_output, attention, mlp)
    futil = _token_step_futil(block_artifact.futil)
    source_memories = [
        name
        for name in block_artifact.provenance["host_preload_memories"]
        if name != "block_input_q16_16"
    ] + ["input_state_q16_16"]
    hardware_memories = list(
        dict.fromkeys(
            [
                *block_artifact.provenance["hardware_owned_memories"],
                "block_input_q16_16",
                "output_state_q16_16",
            ]
        )
    )
    return CalyxArtifact(
        futil=futil,
        provenance={
            "generated": TOKEN_STEP_SCHEMA,
            "futil_sha256": hashlib.sha256(futil.encode("utf-8")).hexdigest(),
            "block_datapath_futil_sha256": block_artifact.provenance[
                "futil_sha256"
            ],
            "block_datapath_provenance": block_artifact.provenance,
            "component_count": 1,
            "host_preload_memories": source_memories,
            "hardware_owned_memories": hardware_memories,
            "state_memories": ["input_state_q16_16", "output_state_q16_16"],
            "interface": {
                "start": "request_when_idle",
                "valid": "qualifies_input_state_capture",
                "done": "asserted_after_output_state_commit",
                "reset": "clears_control_and_transaction_count",
            },
            "expected_transactions": [
                {
                    "index": 0,
                    "input_state_sha256": _little_endian_i64_sha256(first_input),
                    "output_state_sha256": _little_endian_i64_sha256(first_output),
                },
                {
                    "index": 1,
                    "input_state_sha256": _little_endian_i64_sha256(first_output),
                    "output_state_sha256": _little_endian_i64_sha256(second_output),
                },
            ],
            "calyx_disabled_passes": ["cell-share"],
            "host_intermediate": False,
            "host_expected_output_preload": False,
        },
    )


def _cpp_values(values: list[int]) -> str:
    return ",".join(str(value) for value in values)


def _generated_harness(
    artifact: CalyxArtifact,
    block: dict[str, Any],
    attention: dict[str, Any],
    mlp: dict[str, Any],
) -> str:
    block_lowerer = _block_lowerer()
    block_sources, block_computed, _ = block_lowerer._memory_contracts(
        block_lowerer._attention_lowerer(), block_lowerer._mlp_lowerer()
    )
    initial_state = _flatten(attention["tensors"]["block_input_q16_16"]["values"])
    declarations = [
        "// Initial transaction state; no expected outputs are embedded.\n"
        f"static const std::int64_t kInitialState[{len(initial_state)}] = "
        f"{{{_cpp_values(initial_state)}}};"
    ]
    preloads = [
        "  for (unsigned i = 0; i < 256; ++i)\n"
        "    root->main__DOT__input_state_q16_16__DOT__mem[i] = "
        "static_cast<std::uint64_t>(kInitialState[i]);"
    ]
    source_index = 0
    for name in block_sources:
        if name == "block_input_q16_16":
            continue
        values = _flatten(block_lowerer._record_for(name, block, attention, mlp)["values"])
        cpp_name = f"kSource{source_index:02d}"
        source_index += 1
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
    zeroes: list[str] = []
    for name in dict.fromkeys(
        [*block_computed, "block_input_q16_16", "output_state_q16_16"]
    ):
        if name == "output_state_q16_16" or name == "block_input_q16_16":
            count = 256
        else:
            record = block_lowerer._record_for(name, block, attention, mlp)
            count = len(_flatten(record["values"]))
        zeroes.append(
            f"  for (unsigned i = 0; i < {count}; ++i)\n"
            f"    root->main__DOT__{name}__DOT__mem[i] = 0;"
        )
    harness = f'''// Generated source-only stateful token-step harness.
// Only immutable sources and the initial input state are host-written.
#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"

#include <cstdint>
#include <iostream>
#include <vector>

{chr(10).join(declarations)}

static void tick(Vmain& model) {{
  model.clk = 0;
  model.eval();
  model.clk = 1;
  model.eval();
}}

static std::vector<std::int64_t> read_state(Vmain___024root* root, bool output) {{
  std::vector<std::int64_t> values;
  values.reserve(256);
  for (unsigned i = 0; i < 256; ++i) {{
    const std::uint64_t raw = output
      ? root->main__DOT__output_state_q16_16__DOT__mem[i]
      : root->main__DOT__input_state_q16_16__DOT__mem[i];
    values.push_back(static_cast<std::int64_t>(raw));
  }}
  return values;
}}

static void print_vector(const std::vector<std::int64_t>& values) {{
  std::cout << '[';
  for (unsigned i = 0; i < values.size(); ++i) {{
    if (i) std::cout << ',';
    std::cout << values[i];
  }}
  std::cout << ']';
}}

int main(int argc, char** argv) {{
  Verilated::commandArgs(argc, argv);
  Vmain model;
  auto* root = model.rootp;
{chr(10).join(preloads)}
{chr(10).join(zeroes)}

  model.reset = 1;
  model.start = 0;
  model.valid = 0;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  const bool reset_isolated = !model.done &&
    read_state(root, true) == std::vector<std::int64_t>(256, 0);
  model.reset = 0;

  model.valid = 1;
  for (unsigned i = 0; i < 2; ++i) tick(model);
  const bool idle_isolated = !model.done &&
    read_state(root, true) == std::vector<std::int64_t>(256, 0);

  model.valid = 0;
  model.start = 1;
  for (unsigned i = 0; i < 2; ++i) tick(model);
  const bool invalid_isolated = !model.done &&
    read_state(root, true) == std::vector<std::int64_t>(256, 0);
  model.valid = 1;
  const auto first_input = read_state(root, false);
  unsigned first_cycles = 2;
  while (!model.done && first_cycles < 10000000) {{
    tick(model);
    ++first_cycles;
  }}
  if (!model.done) return 2;
  const auto first_output = read_state(root, true);

  model.start = 0;
  model.valid = 0;
  tick(model);
  tick(model);
  const auto second_input = read_state(root, true);
  model.start = 1;
  model.valid = 1;
  unsigned second_cycles = 0;
  while (!model.done && second_cycles < 10000000) {{
    tick(model);
    ++second_cycles;
  }}
  if (!model.done) return 3;
  const auto second_output = read_state(root, true);

  model.start = 0;
  model.valid = 0;
  tick(model);
  tick(model);
  model.reset = 1;
  for (unsigned i = 0; i < 3; ++i) tick(model);
  const bool post_reset_isolated = !model.done;
  model.reset = 0;
  tick(model);
  const auto restart_input = read_state(root, false);
  model.start = 1;
  model.valid = 1;
  unsigned restart_cycles = 0;
  while (!model.done && restart_cycles < 10000000) {{
    tick(model);
    ++restart_cycles;
  }}
  if (!model.done) return 4;
  const auto restart_output = read_state(root, true);
  const bool restart_reselected_initial = restart_input == first_input;
  const bool restart_reproduced_first = restart_output == first_output;
  const bool complete = reset_isolated && idle_isolated && invalid_isolated &&
    post_reset_isolated && restart_reselected_initial &&
    restart_reproduced_first;

  std::cout << "{{\\\"status\\\":\\\"" << (complete ? "ok" : "isolation-failed")
            << "\\\",\\\"reset_isolated\\\":" << (reset_isolated ? "true" : "false")
            << ",\\\"idle_isolated\\\":" << (idle_isolated ? "true" : "false")
            << ",\\\"invalid_isolated\\\":" << (invalid_isolated ? "true" : "false")
            << ",\\\"post_reset_isolated\\\":" << (post_reset_isolated ? "true" : "false")
            << ",\\\"restart_reselected_initial\\\":" << (restart_reselected_initial ? "true" : "false")
            << ",\\\"restart_reproduced_first\\\":" << (restart_reproduced_first ? "true" : "false")
            << ",\\\"first_cycles\\\":" << first_cycles
            << ",\\\"second_cycles\\\":" << second_cycles
            << ",\\\"restart_cycles\\\":" << restart_cycles
            << ",\\\"first_input\\\":";
  print_vector(first_input);
  std::cout << ",\\\"first_output\\\":";
  print_vector(first_output);
  std::cout << ",\\\"second_input\\\":";
  print_vector(second_input);
  std::cout << ",\\\"second_output\\\":";
  print_vector(second_output);
  std::cout << ",\\\"restart_input\\\":";
  print_vector(restart_input);
  std::cout << ",\\\"restart_output\\\":";
  print_vector(restart_output);
  std::cout << "}}\\n";
  return complete ? 0 : 1;
}}
'''
    if "kExpected" in harness:
        raise ValueError("token-step harness contains an expected-output oracle")
    if harness.count("kInitialState[") != 2:
        raise ValueError("token-step initial-state preload contract mismatch")
    for name in artifact.provenance["hardware_owned_memories"]:
        assignment = rf"main__DOT__{re.escape(name)}__DOT__mem\[i\]\s*=\s*([^;]+);"
        if re.findall(assignment, harness) != ["0"]:
            raise ValueError(f"host writes token-step hardware state: {name}")
    return harness


def _artifact_record(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _validate_state(values: object, label: str) -> list[int]:
    if not isinstance(values, list) or len(values) != 256 or not all(
        isinstance(value, int) for value in values
    ):
        raise RuntimeError(f"token-step {label} is not a 256-word integer state")
    return values


def _validate_receipt(
    receipt: dict[str, object], artifact: CalyxArtifact, *, verify_artifacts: bool
) -> dict[str, object]:
    required_fields = {
        "schema",
        "fixture_authorities",
        "block_datapath",
        "interface",
        "reset",
        "transactions",
        "execution",
        "generated_artifacts",
        "calyx_compile_policy",
        "synthesis",
        "receipt_sha256",
    }
    if set(receipt) != required_fields:
        raise ValueError("token-step receipt field set mismatch")
    supplied_hash = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if supplied_hash != _canonical_sha256(unsigned):
        raise ValueError("token-step receipt self-hash mismatch")
    if receipt.get("schema") != TOKEN_STEP_SCHEMA:
        raise ValueError("token-step receipt schema mismatch")
    block_provenance = artifact.provenance["block_datapath_provenance"]
    expected_fixture_authorities = {
        "block": block_provenance["block_fixture_authority"],
        "attention": block_provenance["attention_fixture_authority"],
        "mlp": block_provenance["mlp_fixture_authority"],
    }
    if receipt.get("fixture_authorities") != expected_fixture_authorities:
        raise ValueError("token-step receipt fixture authorities mismatch")
    if receipt.get("block_datapath") != {
        "unchanged_futil_sha256": artifact.provenance[
            "block_datapath_futil_sha256"
        ],
        "wrapper_relationship": "state_capture_then_exact_block_then_state_commit",
    }:
        raise ValueError("token-step receipt block datapath mismatch")
    if receipt.get("interface") != artifact.provenance["interface"]:
        raise ValueError("token-step receipt interface mismatch")
    if receipt.get("transactions") != artifact.provenance["expected_transactions"]:
        raise ValueError("token-step receipt transaction hashes mismatch")
    transactions = receipt["transactions"]
    assert isinstance(transactions, list)
    if transactions[1]["input_state_sha256"] != transactions[0]["output_state_sha256"]:
        raise ValueError("token-step feedback hash mismatch")
    reset = receipt.get("reset")
    if not isinstance(reset, dict) or set(reset) != {
        "asserted_cycles",
        "isolated",
        "idle_start_isolated",
        "invalid_input_isolated",
        "post_transaction_restart",
    }:
        raise ValueError("token-step reset/isolation evidence mismatch")
    if (
        reset.get("asserted_cycles") != 3
        or reset.get("isolated") is not True
        or reset.get("idle_start_isolated") is not True
        or reset.get("invalid_input_isolated") is not True
    ):
        raise ValueError("token-step reset/isolation evidence mismatch")
    restart = reset.get("post_transaction_restart")
    if not isinstance(restart, dict) or set(restart) != {
        "asserted_cycles",
        "isolated",
        "reselected_initial_state",
        "reproduced_transaction_0",
        "input_state_sha256",
        "output_state_sha256",
        "cycles",
    }:
        raise ValueError("token-step post-transaction reset evidence mismatch")
    if (
        restart.get("asserted_cycles") != 3
        or restart.get("isolated") is not True
        or restart.get("reselected_initial_state") is not True
        or restart.get("reproduced_transaction_0") is not True
        or restart.get("input_state_sha256")
        != transactions[0]["input_state_sha256"]
        or restart.get("output_state_sha256")
        != transactions[0]["output_state_sha256"]
        or not isinstance(restart.get("cycles"), int)
        or restart["cycles"] <= 131072
    ):
        raise ValueError("token-step post-transaction reset evidence mismatch")
    execution = receipt.get("execution")
    if not isinstance(execution, dict) or execution.get("component_count") != 1:
        raise ValueError("token-step execution component mismatch")
    if set(execution) != {
        "component_count",
        "simulator_runs",
        "transaction_count",
        "verification_invocations",
        "cycles",
        "host_preload_memories",
        "hardware_owned_memories",
        "direct_handoff",
        "host_intermediate",
        "host_expected_output_preload",
    }:
        raise ValueError("token-step execution field set mismatch")
    if (
        execution.get("simulator_runs") != 1
        or execution.get("transaction_count") != 2
        or execution.get("verification_invocations") != 3
    ):
        raise ValueError("token-step execution transaction mismatch")
    cycles = execution.get("cycles")
    if (
        not isinstance(cycles, list)
        or len(cycles) != 2
        or not all(isinstance(value, int) and value > 131072 for value in cycles)
    ):
        raise ValueError("token-step execution cycles mismatch")
    if execution.get("host_preload_memories") != artifact.provenance[
        "host_preload_memories"
    ] or execution.get("hardware_owned_memories") != artifact.provenance[
        "hardware_owned_memories"
    ]:
        raise ValueError("token-step execution memory ownership mismatch")
    if execution.get("direct_handoff") != (
        "transaction_0.output_state_q16_16_to_transaction_1.block_input_q16_16"
    ):
        raise ValueError("token-step execution handoff mismatch")
    if execution.get("host_intermediate") is not False or execution.get(
        "host_expected_output_preload"
    ) is not False:
        raise ValueError("token-step host handoff/preload mismatch")
    artifacts = receipt.get("generated_artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "futil",
        "sv",
        "synthesis_sv",
        "harness",
    }:
        raise ValueError("token-step artifact record mismatch")
    expected_paths = {
        "futil": ARTIFACT_DIRECTORY / "token-step.futil",
        "sv": ARTIFACT_DIRECTORY / "main.sv",
        "synthesis_sv": ARTIFACT_DIRECTORY / "main-synthesis.sv",
        "harness": ARTIFACT_DIRECTORY / "harness.cpp",
    }
    digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    for name, expected_path in expected_paths.items():
        record = artifacts.get(name)
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "bytes", "sha256"}
            or record.get("path") != str(expected_path)
            or not isinstance(record.get("bytes"), int)
            or record["bytes"] <= 0
            or not isinstance(record.get("sha256"), str)
            or digest_pattern.fullmatch(record["sha256"]) is None
        ):
            raise ValueError(f"token-step {name} artifact record invalid")
    if artifacts["futil"]["sha256"] != artifact.provenance["futil_sha256"]:
        raise ValueError("token-step receipt Futil hash mismatch")
    if artifacts["futil"]["bytes"] != len(artifact.futil.encode("utf-8")):
        raise ValueError("token-step receipt Futil byte count mismatch")
    if verify_artifacts:
        for name, record in artifacts.items():
            if not isinstance(record, dict):
                raise ValueError(f"token-step {name} artifact record invalid")
            path = Path(str(record.get("path")))
            if not path.is_file() or _artifact_record(path) != record:
                raise ValueError(f"token-step {name} artifact hash mismatch")
        if Path(str(artifacts["futil"]["path"])).read_text(encoding="utf-8") != artifact.futil:
            raise ValueError("token-step Futil bytes differ from authority")
    synthesis = receipt.get("synthesis")
    if not isinstance(synthesis, dict) or set(synthesis) != {
        "status",
        "same_futil_sha256",
        "synthesis_sv_sha256",
        "command",
        "resources",
    } or synthesis.get("status") != "passed":
        raise ValueError("token-step synthesis status mismatch")
    if synthesis.get("same_futil_sha256") != artifacts["futil"]["sha256"]:
        raise ValueError("token-step synthesis did not consume the simulated Futil")
    if synthesis.get("synthesis_sv_sha256") != artifacts["synthesis_sv"]["sha256"]:
        raise ValueError("token-step synthesis SV hash mismatch")
    expected_yosys_command = [
        "yosys",
        "-p",
        f"read_verilog -sv {expected_paths['synthesis_sv']}; "
        "hierarchy -check -top main; stat",
    ]
    if synthesis.get("command") != expected_yosys_command:
        raise ValueError("token-step Yosys command mismatch")
    resources = synthesis.get("resources")
    if (
        not isinstance(resources, dict)
        or set(resources) != {"cells", "memories", "memory_bits"}
        or not all(isinstance(value, int) and value > 0 for value in resources.values())
    ):
        raise ValueError("token-step Yosys resource evidence mismatch")
    block_lowerer = _block_lowerer()
    expected_common = block_lowerer._calyx_common_command(expected_paths["futil"])
    compile_policy = receipt.get("calyx_compile_policy")
    if compile_policy != {
        "disabled_passes": ["cell-share"],
        "simulator_command": [
            *expected_common,
            "-b",
            "verilog",
            "-o",
            str(expected_paths["sv"]),
        ],
        "synthesis_command": [
            *expected_common,
            "--synthesis",
            "--disable-verify",
            "-b",
            "verilog",
            "-o",
            str(expected_paths["synthesis_sv"]),
        ],
        "same_futil_sha256": artifacts["futil"]["sha256"],
    }:
        raise ValueError("token-step Calyx compile policy mismatch")
    return receipt


def run_token_step_sv(
    artifact: CalyxArtifact,
    block_fixture: Path,
    attention_fixture: Path,
    mlp_fixture: Path,
) -> dict[str, object]:
    """Run two transactions in one simulation and bind same-Futil synthesis."""
    block_lowerer = _block_lowerer()
    expected = generate_token_step_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    if artifact != expected:
        raise ValueError("token-step artifact fixture/schema authority mismatch")
    block, attention, mlp = block_lowerer._checked_fixtures(
        block_fixture, attention_fixture, mlp_fixture
    )
    artifact_dir = ARTIFACT_DIRECTORY
    artifact_dir.mkdir(parents=True, exist_ok=True)
    futil_path = artifact_dir / "token-step.futil"
    sv_path = artifact_dir / "main.sv"
    synthesis_sv_path = artifact_dir / "main-synthesis.sv"
    harness_path = artifact_dir / "harness.cpp"
    futil_path.write_text(artifact.futil, encoding="utf-8")
    harness_path.write_text(
        _generated_harness(artifact, block, attention, mlp), encoding="utf-8"
    )

    common = block_lowerer._calyx_common_command(futil_path)
    simulator_command = [*common, "-b", "verilog", "-o", str(sv_path)]
    synthesis_command = [
        *common,
        "--synthesis",
        "--disable-verify",
        "-b",
        "verilog",
        "-o",
        str(synthesis_sv_path),
    ]
    compiled = subprocess.run(
        simulator_command, text=True, capture_output=True, check=False, timeout=600
    )
    if compiled.returncode != 0 or not sv_path.is_file():
        raise RuntimeError(f"token-step Calyx-to-SV failed: {compiled.stderr.strip()}")
    verilator_dir = artifact_dir / "verilator"
    executable = verilator_dir / "fixed_point_token_step_harness"
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
        verilator_command, text=True, capture_output=True, check=False, timeout=900
    )
    if verilated.returncode != 0 or not executable.is_file():
        raise RuntimeError(f"token-step Verilator build failed: {verilated.stderr.strip()}")
    simulated = subprocess.run(
        [str(executable)], text=True, capture_output=True, check=False, timeout=1200
    )
    if simulated.returncode != 0:
        raise RuntimeError(
            "generated-SV token-step harness failed: "
            f"{simulated.stdout[-2000:]} {simulated.stderr.strip()}"
        )
    try:
        raw = json.loads(
            next(line for line in reversed(simulated.stdout.splitlines()) if line.startswith("{"))
        )
    except (json.JSONDecodeError, StopIteration) as error:
        raise RuntimeError("token-step harness did not emit JSON") from error
    if raw.get("status") != "ok":
        raise RuntimeError("token-step reset/valid isolation failed")
    first_input = _validate_state(raw.get("first_input"), "first input")
    first_output = _validate_state(raw.get("first_output"), "first output")
    second_input = _validate_state(raw.get("second_input"), "second input")
    second_output = _validate_state(raw.get("second_output"), "second output")
    restart_input = _validate_state(raw.get("restart_input"), "restart input")
    restart_output = _validate_state(raw.get("restart_output"), "restart output")
    observed_transactions = [
        {
            "index": 0,
            "input_state_sha256": _little_endian_i64_sha256(first_input),
            "output_state_sha256": _little_endian_i64_sha256(first_output),
        },
        {
            "index": 1,
            "input_state_sha256": _little_endian_i64_sha256(second_input),
            "output_state_sha256": _little_endian_i64_sha256(second_output),
        },
    ]
    if second_input != first_output:
        raise RuntimeError("token-step second input is not committed first output")
    if observed_transactions != artifact.provenance["expected_transactions"]:
        raise RuntimeError("token-step generated-SV state differs from fixed-point oracle")
    if restart_input != first_input or restart_output != first_output:
        raise RuntimeError(
            "token-step post-transaction reset did not restart from input state"
        )

    synthesized = subprocess.run(
        synthesis_command, text=True, capture_output=True, check=False, timeout=600
    )
    if synthesized.returncode != 0 or not synthesis_sv_path.is_file():
        raise RuntimeError(
            f"token-step Calyx synthesis-to-SV failed: {synthesized.stderr.strip()}"
        )
    generated_artifacts = {
        "futil": _artifact_record(futil_path),
        "sv": _artifact_record(sv_path),
        "synthesis_sv": _artifact_record(synthesis_sv_path),
        "harness": _artifact_record(harness_path),
    }
    if generated_artifacts["futil"]["sha256"] != artifact.provenance["futil_sha256"]:
        raise RuntimeError("token-step same-Futil hash mismatch")
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
        raise RuntimeError(f"token-step Yosys stat failed: {yosys.stderr.strip()}")
    resources = block_lowerer._validate_yosys_stat(yosys.stdout)
    receipt: dict[str, object] = {
        "schema": TOKEN_STEP_SCHEMA,
        "fixture_authorities": {
            "block": block_lowerer._fixture_authority(block),
            "attention": block_lowerer._fixture_authority(attention),
            "mlp": block_lowerer._fixture_authority(mlp),
        },
        "block_datapath": {
            "unchanged_futil_sha256": artifact.provenance[
                "block_datapath_futil_sha256"
            ],
            "wrapper_relationship": "state_capture_then_exact_block_then_state_commit",
        },
        "interface": artifact.provenance["interface"],
        "reset": {
            "asserted_cycles": 3,
            "isolated": raw["reset_isolated"],
            "idle_start_isolated": raw["idle_isolated"],
            "invalid_input_isolated": raw["invalid_isolated"],
            "post_transaction_restart": {
                "asserted_cycles": 3,
                "isolated": raw["post_reset_isolated"],
                "reselected_initial_state": raw["restart_reselected_initial"],
                "reproduced_transaction_0": raw["restart_reproduced_first"],
                "input_state_sha256": _little_endian_i64_sha256(restart_input),
                "output_state_sha256": _little_endian_i64_sha256(restart_output),
                "cycles": raw["restart_cycles"],
            },
        },
        "transactions": observed_transactions,
        "execution": {
            "component_count": 1,
            "simulator_runs": 1,
            "transaction_count": 2,
            "verification_invocations": 3,
            "cycles": [raw["first_cycles"], raw["second_cycles"]],
            "host_preload_memories": artifact.provenance["host_preload_memories"],
            "hardware_owned_memories": artifact.provenance[
                "hardware_owned_memories"
            ],
            "direct_handoff": "transaction_0.output_state_q16_16_to_transaction_1.block_input_q16_16",
            "host_intermediate": False,
            "host_expected_output_preload": False,
        },
        "generated_artifacts": generated_artifacts,
        "calyx_compile_policy": {
            "disabled_passes": ["cell-share"],
            "simulator_command": simulator_command,
            "synthesis_command": synthesis_command,
            "same_futil_sha256": generated_artifacts["futil"]["sha256"],
        },
        "synthesis": {
            "status": "passed",
            "same_futil_sha256": generated_artifacts["futil"]["sha256"],
            "synthesis_sv_sha256": generated_artifacts["synthesis_sv"]["sha256"],
            "command": yosys_command,
            "resources": resources,
        },
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return _validate_receipt(receipt, artifact, verify_artifacts=True)


def validate_token_step_receipt(
    receipt: dict[str, object],
    block_fixture: Path,
    attention_fixture: Path,
    mlp_fixture: Path,
    *,
    verify_artifacts: bool = True,
) -> dict[str, object]:
    artifact = generate_token_step_kernel(
        block_fixture, attention_fixture, mlp_fixture
    )
    return _validate_receipt(receipt, artifact, verify_artifacts=verify_artifacts)
