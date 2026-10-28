#!/usr/bin/env python3
"""Execute the compiler-generated family SV against its pinned first-token oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


def compare_trace(oracle: dict, trace: dict) -> dict[str, object]:
    if "token" not in trace or type(trace["token"]) is not int:
        raise ValueError("missing RTL token")
    prompt = oracle.get("prompt_ids")
    if trace.get("prompt_ids") != prompt:
        raise ValueError("RTL prompt IDs differ from pinned oracle")
    if type(trace.get("cycles")) is not int or trace["cycles"] <= 0:
        raise ValueError("RTL trace lacks a positive cycle count")
    checkpoints = oracle.get("first_token", {}).get("checkpoints", {})
    if "block.0.output" in checkpoints:
        expected_block = checkpoints["block.0.output"]
        actual_block = trace.get("checkpoints", {}).get("block.0.output")
        if not isinstance(actual_block, list) or len(actual_block) != len(expected_block):
            raise ValueError("missing RTL checkpoint block.0.output")
        for index, (expected_value, actual_value) in enumerate(zip(expected_block, actual_block)):
            if actual_value != expected_value:
                return {"status": "mismatched", "first_mismatch": {
                    "checkpoint": "block.0.output", "index": index,
                    "expected": expected_value, "actual": actual_value}}
    expected = oracle["tokens"][0]
    if trace["token"] != expected:
        return {"status": "mismatched", "first_mismatch": {
            "checkpoint": "token_0", "expected": expected, "actual": trace["token"]}}
    return {"status": "matched", "first_token": expected, "cycles": trace["cycles"]}


def _harness(prompt_ids: list[int], max_cycles: int, hidden: int,
             expected_block: list[int], progress_interval: int) -> str:
    return r'''#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"
#include <cstdint>
#include <fstream>
#include <iostream>
#include <vector>
int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  if (argc != 2) return 2;
  std::ofstream progress(argv[1]);
  if (!progress) return 2;
  Vmain model;
  model.prompt_0 = PROMPT_0;
  model.prompt_1 = PROMPT_1;
  model.prompt_2 = PROMPT_2;
  model.prompt_3 = PROMPT_3;
  model.token_index = 0;
  model.reset = 1; model.start = 0; model.valid = 0;
  for (int i = 0; i < 3; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
  model.reset = 0; model.valid = 1;
  for (int i = 0; i < 3; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
  model.start = 1; model.valid = 0;
  for (int i = 0; i < 4; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
  model.valid = 1;
  std::uint64_t cycles = 0;
  auto* root = model.rootp;
  std::vector<std::int64_t> block0(HIDDEN);
  const std::int64_t expected_block[HIDDEN] = {EXPECTED_BLOCK};
  bool block0_captured = false;
  while (!model.done && cycles < MAX_CYCLES) {
    model.clk = 0; model.eval(); model.clk = 1; model.eval(); ++cycles;
    if ((cycles % PROGRESS_INTERVAL) == 0)
      progress << "{\"event\":\"progress\",\"cycles\":" << cycles << "}" << std::endl;
    if (!block0_captured && root->main__DOT__model_layer_out == 1) {
      for (unsigned column = 0; column < HIDDEN; ++column) {
        block0[column] = static_cast<std::int64_t>(
          root->main__DOT__block_output_q16_16__DOT__mem[(3 * HIDDEN) + column]);
        if (block0[column] != expected_block[column]) {
          std::cerr << "block.0.output mismatch at " << column << ": expected "
                    << expected_block[column] << ", actual " << block0[column] << "\n";
          return 5;
        }
      }
      block0_captured = true;
      progress << "{\"event\":\"block0_match\",\"cycles\":" << cycles << "}" << std::endl;
    }
  }
  if (!model.done) { std::cerr << "first-token RTL timeout after " << cycles << " cycles\n"; return 3; }
  if (!block0_captured) { std::cerr << "missing block-0 output checkpoint\n"; return 4; }
  model.token_index = 0; model.clk = 0; model.eval();
  std::cout << "{\"token\":" << unsigned(model.token_id)
            << ",\"cycles\":" << cycles
            << ",\"prompt_ids\":[PROMPT_IDS]"
            << ",\"checkpoints\":{\"block.0.output\":[";
  for (unsigned column = 0; column < HIDDEN; ++column) {
    if (column) std::cout << ',';
    std::cout << block0[column];
  }
  std::cout << "]}}\n";
  return 0;
}
'''.replace("PROMPT_IDS", ",".join(map(str, prompt_ids))) \
   .replace("MAX_CYCLES", str(max_cycles)) \
   .replace("PROGRESS_INTERVAL", str(progress_interval)) \
   .replace("EXPECTED_BLOCK", ",".join(map(str, expected_block))) \
   .replace("HIDDEN", str(hidden)) \
   .replace("PROMPT_0", str(prompt_ids[0])) \
   .replace("PROMPT_1", str(prompt_ids[1])) \
   .replace("PROMPT_2", str(prompt_ids[2])) \
   .replace("PROMPT_3", str(prompt_ids[3]))


def _run_harness(executable: Path, image: Path, *, timeout: int,
                 progress: Path | None = None) -> list[str]:
    command = [str(executable)] + ([str(progress)] if progress is not None else [])
    completed = subprocess.run(command, cwd=image, capture_output=True,
                               text=True, timeout=timeout, check=False)
    if completed.returncode:
        raise ValueError(
            f"RTL simulator exited {completed.returncode}; "
            f"stderr: {completed.stderr[-4000:]}; stdout: {completed.stdout[-1000:]}"
        )
    return completed.stdout.strip().splitlines()


def run(image: Path, oracle_path: Path, output: Path, *, max_cycles: int,
        compile_timeout: int = 1800, run_timeout: int = 7200,
        progress_interval: int = 10_000_000) -> dict[str, object]:
    oracle = json.loads(oracle_path.read_text())
    prompt_ids = oracle["prompt_ids"]
    if len(prompt_ids) != 4 or any(type(value) is not int or not 0 <= value < 65536
                                   for value in prompt_ids):
        raise ValueError("pinned oracle requires four 16-bit prompt IDs")
    output.mkdir(parents=True, exist_ok=True)
    harness = output / "harness.cpp"
    hidden = oracle["hidden"]
    if type(hidden) is not int or hidden < 1:
        raise ValueError("pinned oracle requires a positive hidden width")
    expected_block = oracle["first_token"]["checkpoints"]["block.0.output"]
    if (not isinstance(expected_block, list) or len(expected_block) != hidden
            or any(type(value) is not int for value in expected_block)):
        raise ValueError("pinned oracle lacks a complete integer block-0 checkpoint")
    if type(progress_interval) is not int or progress_interval < 1:
        raise ValueError("progress interval must be a positive integer")
    harness.write_text(_harness(prompt_ids, max_cycles, hidden,
                                expected_block, progress_interval))
    visibility = output / "visibility.vlt"
    visibility.write_text(
        '`verilator_config\n'
        'public_flat_rw -module "seq_mem_d1" -var "mem" @(posedge clk)\n'
        'public_flat_rd -module "main" -var "model_layer_out"\n'
    )
    build = output / "verilator"
    subprocess.run([
        "verilator", "--cc", "--exe", "--build", "-j", "4",
        "--top-module", "main", "--Mdir", str(build),
        "-CFLAGS", "-std=c++17 -O3", "-o", "family_harness",
        str(visibility), str(image / "model-static-init-normalized.sv"), str(harness),
    ], check=True, timeout=compile_timeout)
    lines = _run_harness(build / "family_harness", image, timeout=run_timeout,
                         progress=output / "progress.jsonl")
    if len(lines) != 1:
        raise ValueError(f"RTL simulator emitted {len(lines)} trace records, expected one")
    trace = json.loads(lines[0])
    result = compare_trace(oracle, trace)
    (output / "trace.json").write_text(json.dumps(trace, sort_keys=True, indent=2) + "\n")
    (output / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    if result["status"] != "matched":
        raise ValueError(f"first generated-RTL mismatch: {result['first_mismatch']}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cycles", type=int, default=2_000_000_000)
    args = parser.parse_args()
    run(args.image, args.oracle, args.output, max_cycles=args.max_cycles)


if __name__ == "__main__":
    main()
