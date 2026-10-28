#!/usr/bin/env python3
"""Run the audit-free TinyStories-1M generated RTL from source tensors only."""
from __future__ import annotations

import hashlib
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import re

ROOT = Path(__file__).resolve().parents[2]
LUTRAM_MEMORIES = {
    "attention_context_q16_16", "attention_residual_q16_16",
    "block_input_q16_16", "block_output_q16_16", "ln1_output_q16_16",
    "ln2_output_q16_16", "final_output_q16_16",
    # These are single-consumer, transient working buffers.  Keeping them in
    # distributed RAM avoids spending a whole RAMB36 on each 1,280-word
    # activation while leaving immutable model weights in block RAM.
    "q_input_codes_i8", "q_input_q16_16", "q_accumulator_i64",
    "q_output_q16_16", "k_input_q16_16", "k_output_q16_16",
    "v_input_q16_16", "v_output_q16_16", "out_input_q16_16",
    "out_output_q16_16", "c_fc_input_codes_i8", "c_fc_input_q16_16",
    "c_proj_input_q16_16", "c_proj_output_q16_16",
    "attention_score_q8", "attention_delta_q8", "attention_exp_q1_20",
    "c_fc_output_codes_i8", "c_fc_output_q16_16",
    "c_proj_post_weight_rescale_bias_q16_16", "lm_input_q16_16",
    # Only c_fc's large immutable ROM uses BRAM. The exact one-ROM board
    # diagnostic restored first-token 11; mapping c_proj to BRAM returned 547.
    # Keep c_proj and the four token-scale banks distributed.
    "c_proj_weight_codes_i8",
    "token_weight_scale_q8_24_b0", "token_weight_scale_q8_24_b1",
    "token_weight_scale_q8_24_b2", "token_weight_scale_q8_24_b3",
}


def prepare_production_library(install: Path, directory: Path) -> Path:
    """Declare the latency of the production SV memory post-pass to Calyx.

    Large memories gain an address stage; small memories retain one-cycle
    hardware but safely hold their result for the conservative two-cycle
    schedule. This private metadata overlay never modifies installed RTL.
    """
    source = install / "share/calyx"
    declaration = source / "primitives/memories/seq.futil"
    original = declaration.read_text()
    start = original.index("  primitive seq_mem_d1[")
    end = original.index("  primitive seq_mem_d2[")
    d1 = original[start:end]
    if d1.count("@interval(1)") != 1:
        raise ValueError("unexpected seq_mem_d1 latency declaration")
    modified = original[:start] + d1.replace("@interval(1)", "@interval(2)") + original[end:]
    modified = "// Production override: seq_mem_d1 scheduled for two cycles.\n" + modified
    # Create writable private directories, not copytree's read-only Nix modes.
    # Unlink only the private declaration; never write through a store link.
    directory.mkdir()
    for entry in source.rglob("*"):
        destination = directory / entry.relative_to(source)
        if entry.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(entry.resolve())
    target = directory / "primitives/memories/seq.futil"
    target.unlink()
    target.write_text(modified)
    receipt = {
        "schema": "tinystories-production-memory-latency-v1",
        "seq_mem_d1_schedule_cycles": 2,
        "installed_declaration_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "production_declaration_sha256": hashlib.sha256(modified.encode()).hexdigest(),
        "memory_rtl_modified": False,
    }
    receipt["receipt_sha256"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (directory / "production-memory-latency.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return directory


def annotate_bram_memories(source: str) -> str:
    """Preserve the production memory contract through Calyx SV emission.

    Calyx's generic sequential-memory primitive does not carry a Xilinx RAM
    style attribute.  Without this post-pass, Yosys is free to choose LUTRAM
    or FF mapping for the 16K banked model tables.  The attribute is purely a
    synthesis hint; simulation semantics and memory contents are unchanged.
    """
    needle = "  logic [WIDTH-1:0] mem[SIZE-1:0];"
    if needle not in source:
        return source
    # Keep the generic primitive available for small asynchronous/control
    # memories.  A separate variant carries the Xilinx BRAM hint, and use it
    # for substantial buffers (at least 512 words) up to the 72-bit width
    # supported by the Xilinx block-RAM descriptor.  This includes the
    # compiler-generated 64-bit runtime buffers that otherwise become
    # prohibitively large FF/mux networks.
    marker = "module seq_mem_d1_bram #"
    if marker not in source:
        start = source.index("module seq_mem_d1 #")
        end = source.index("endmodule", start) + len("endmodule")
        body = source[start:end].replace("module seq_mem_d1 #", marker, 1)
        body = body.replace(needle, '  (* ram_style = "block" *)\n' + needle, 1)
        # Make the address staging explicit for Xilinx BRAM inference.  The
        # generic Calyx helper registers only read_data. The extra address
        # stage changes the read latency to two cycles, which must be declared
        # before scheduling through prepare_production_library.
        body = body.replace(
            "  logic [WIDTH-1:0] read_out;",
            "  logic [WIDTH-1:0] read_out;\n  logic [IDX_SIZE-1:0] read_addr;\n  logic read_pending;",
            1,
        )
        body = body.replace(
            "  always_ff @(posedge clk) begin\n    if (reset) begin\n      read_out <= '0;",
            "  always_ff @(posedge clk) begin\n    read_addr <= addr0;\n    if (reset) begin\n      read_addr <= '0;\n      read_out <= '0;",
            1,
        )
        body = body.replace("read_out <= mem[addr0];", "read_out <= mem[read_addr];", 1)
        body = body.replace(
            "  always_ff @(posedge clk) begin\n    if (reset) begin\n      done <= '0;\n    end else if (content_en) begin\n      done <= '1;\n    end else begin\n      done <= '0;\n    end\n  end",
            "  always_ff @(posedge clk) begin\n    if (reset) begin\n      done <= '0;\n      read_pending <= '0;\n    end else begin\n      done <= read_pending;\n      read_pending <= content_en;\n    end\n  end",
            1,
        )
        source = source[:end] + "\n\n" + body + source[end:]
        lut_marker = "module seq_mem_d1_lutram #"
        lut_body = body.replace(marker, lut_marker, 1).replace(
            '  (* ram_style = "block" *)\n',
            '  (* ram_style = "distributed" *)\n', 1)
        source = source.replace(body, body + "\n\n" + lut_body, 1)
    # Match each parameterized instance and inspect its literal SIZE.  This
    # intentionally leaves non-literal/generated forms untouched.
    pattern = re.compile(r"seq_mem_d1\s*#\s*\(", re.S)
    # Skip the primitive definitions themselves; only rewrite instantiations.
    cursor = source.index("endmodule", source.index("module seq_mem_d1 #")) + len("endmodule")
    pieces: list[str] = [source[:cursor]]
    while (match := pattern.search(source, cursor)):
        open_paren = match.end() - 1
        depth = 1
        pos = open_paren + 1
        while pos < len(source) and depth:
            depth += source[pos] == "("
            depth -= source[pos] == ")"
            pos += 1
        if depth:
            break
        tail = re.match(r"\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(", source[pos:])
        if not tail:
            cursor = pos
            continue
        params = source[open_paren + 1 : pos - 1]
        pieces.append(source[cursor : match.start()])
        size_match = re.search(r"\.SIZE\s*\(\s*(\d+)\s*\)", params)
        width_match = re.search(r"\.WIDTH\s*\(\s*(\d+)\s*\)", params)
        if size_match and width_match and int(size_match.group(1)) >= 512 and int(width_match.group(1)) <= 72:
            variant = "seq_mem_d1_lutram" if tail.group("name") in LUTRAM_MEMORIES else "seq_mem_d1_bram"
            pieces.append(variant + " # (" + params + ") " + tail.group("name") + " (")
        else:
            pieces.append(source[match.start() : pos + tail.end()])
        cursor = pos + tail.end()
    pieces.append(source[cursor:])
    return "".join(pieces)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


production = load(ROOT / "scripts/pipeline/lower_fixed_point_model_bram_production.py", "bram_production")
base = production.base


def harness(artifact):
    steps = artifact.provenance["schedule"]["generation_steps"]
    if type(steps) is not int or steps not in (1, 2, 16):
        raise ValueError("generation_steps must be 1, 2, or 16")
    preloads = "\n".join(
        f'  preload(root->main__DOT__{name}__DOT__mem, directory + "/source-{name}.bin");'
        for name in artifact.provenance["source_memories"]
    )
    return r'''#include "Vmain.h"
#include "Vmain___024root.h"
#include "verilated.h"
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

template<class Memory> static void preload(Memory& memory, const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw std::runtime_error("missing source: " + path);
  input.seekg(0, std::ios::end);
  const auto words = sizeof(memory.m_storage) / sizeof(memory.m_storage[0]);
  const auto expected = static_cast<std::streamoff>(words * sizeof(std::uint64_t));
  if (input.tellg() != expected) throw std::runtime_error("source size mismatch: " + path);
  input.seekg(0, std::ios::beg);
  std::vector<unsigned char> bytes(static_cast<std::size_t>(expected));
  input.read(reinterpret_cast<char*>(bytes.data()), expected);
  if (input.gcount() != expected) throw std::runtime_error("short source: " + path);
  for (std::size_t i = 0; i < words; ++i) {
    std::uint64_t value = 0;
    for (unsigned byte = 0; byte < sizeof(std::uint64_t); ++byte)
      value |= std::uint64_t(bytes[i * sizeof(std::uint64_t) + byte]) << (byte * 8);
    memory.m_storage[i] = value;
  }
}

int main(int argc, char** argv) {
  Verilated::commandArgs(argc, argv);
  if (argc != 2) return 2;
  const std::string directory = argv[1];
  std::cout << "{\"event\":\"construct_begin\"}" << std::endl;
  Vmain model;
  std::cout << "{\"event\":\"construct_done\"}" << std::endl;
  auto* root = model.rootp;
  // The board ABI supplies the authenticated frozen prompt through the same
  // ports driven by the JTAG CSR shell.
  model.prompt_0 = 7454;
  model.prompt_1 = 2402;
  model.prompt_2 = 257;
  model.prompt_3 = 640;
  PRELOADS
  std::cout << "{\"event\":\"preload_done\"}" << std::endl;
  std::uint64_t completed[2] = {0, 0};
  std::uint16_t tokens[2][16] = {};
  constexpr unsigned steps = GENERATION_STEPS;
  const unsigned expected[16] = {11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375, 6151, 284, 711, 2354, 287, 262};
  for (unsigned run = 0; run < 2; ++run) {
    unsigned seen = 0;
    model.token_index = 0;
    model.reset = 1; model.start = 0; model.valid = 0;
    for (unsigned i = 0; i < 3; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
    model.reset = 0; model.valid = 1;
    for (unsigned i = 0; i < 3; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
    model.start = 1; model.valid = 0;
    for (unsigned i = 0; i < 4; ++i) { model.clk = 0; model.eval(); model.clk = 1; model.eval(); }
    model.valid = 1;
    std::cout << "{\"event\":\"started\",\"run\":" << run << "}" << std::endl;
    while (!model.done && completed[run] < 2000000000ULL) {
      model.clk = 0; model.eval();
      const bool selected = root->main__DOT__selected_tokens_write_en &&
        root->main__DOT__selected_tokens_content_en;
      const unsigned step = root->main__DOT__selected_tokens_addr0;
      const bool context_written = root->main__DOT__context_tokens_write_en &&
        root->main__DOT__context_tokens_content_en;
      const unsigned context_address = root->main__DOT__context_tokens_addr0;
      const unsigned context_data = root->main__DOT__context_tokens_write_data;
      model.clk = 1; model.eval(); ++completed[run];
      // Keep long full-model runs observable without producing millions of
      // lines of transport output.
      if ((completed[run] % 1000000ULL) == 0)
        std::cout << "{\"event\":\"progress\",\"run\":" << run
          << ",\"cycles\":" << completed[run] << "}" << std::endl;
      if (selected) {
        if (step >= steps) return 6;
        seen |= 1u << step;
        tokens[run][step] = root->main__DOT__selected_tokens__DOT__mem[step];
        if (!context_written || context_address != 4 + step ||
            context_data != tokens[run][step]) {
          std::cout << "{\"event\":\"context_mismatch\",\"run\":" << run
            << ",\"step\":" << step << ",\"address\":" << context_address
            << ",\"expected\":" << tokens[run][step]
            << ",\"actual\":" << context_data << "}" << std::endl;
          return 8;
        }
        const unsigned prompt[4] = {7454, 2402, 257, 640};
        for (unsigned address = 0; address <= 4 + step; ++address) {
          const unsigned want = address < 4 ? prompt[address] : expected[address - 4];
          const unsigned got = root->main__DOT__context_tokens__DOT__mem[address];
          if (got != want) {
            std::cout << "{\"event\":\"context_mismatch\",\"run\":" << run
              << ",\"step\":" << step << ",\"address\":" << address
              << ",\"expected\":" << want << ",\"actual\":" << got
              << "}" << std::endl;
            return 8;
          }
        }
        std::cout << "{\"event\":\"selected\",\"run\":" << run
          << ",\"step\":" << step << ",\"token\":" << tokens[run][step]
          << ",\"cycles\":" << completed[run] << "}" << std::endl;
      }
    }
    if (!model.done || completed[run] >= 2000000000ULL) return 3;
    if (seen != ((1u << steps) - 1)) return 7;
    for (unsigned step = 0; step < steps; ++step) {
      model.token_index = step;
      model.clk = 0; model.eval();
      if (tokens[run][step] != expected[step] || model.token_id != expected[step]) return 4;
      std::cout << "{\"event\":\"token_port\",\"run\":" << run
        << ",\"step\":" << step << ",\"token\":" << model.token_id << "}" << std::endl;
    }
    std::cout << "{\"event\":\"complete\",\"run\":" << run
      << ",\"cycles\":" << completed[run] << "}" << std::endl;
  }
  if (completed[0] != completed[1]) return 5;
  for (unsigned step = 0; step < steps; ++step)
    if (tokens[0][step] != tokens[1][step]) return 5;
  return 0;
}
'''.replace("PRELOADS", preloads).replace("GENERATION_STEPS", str(steps))


def run_and_record_harness(
    executable: Path,
    directory: Path,
    *,
    generation_steps: int,
    timeout_seconds: int = 7200,
    bindings: dict | None = None,
) -> dict:
    """Preserve the actual simulator stream and accept only two exact runs."""
    expected = [11, 612, 373, 257, 1310, 2576, 3706, 20037,
                13, 1375, 6151, 284, 711, 2354, 287, 262][:generation_steps]
    if generation_steps not in (1, 2, 16):
        raise ValueError("generation_steps must be 1, 2, or 16")
    log_path = directory / "production-execution.jsonl"
    start = time.monotonic()
    with log_path.open("w") as log:
        result = subprocess.run([str(executable), str(directory)], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout_seconds, check=False)
    elapsed = time.monotonic() - start
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    selected = [[], []]
    output_ports = [[], []]
    cycles = [None, None]
    for line in log_path.read_text().splitlines():
        event = json.loads(line)
        kind = event.get("event")
        if kind == "context_mismatch":
            raise ValueError("simulator reported a context mismatch")
        if kind not in ("selected", "token_port", "complete"):
            continue
        run = event.get("run")
        if type(run) is not int or run not in (0, 1):
            raise ValueError("invalid simulator run index")
        if kind in ("selected", "token_port"):
            target = selected if kind == "selected" else output_ports
            if event.get("step") != len(target[run]):
                raise ValueError("missing or unordered token event")
            target[run].append(event.get("token"))
        elif cycles[run] is not None or type(event.get("cycles")) is not int:
            raise ValueError("missing or repeated completion event")
        else:
            cycles[run] = event["cycles"]
    if selected != [expected, expected] or output_ports != [expected, expected]:
        raise ValueError("selected tokens or public output ports differ from the frozen reference")
    if cycles[0] is None or cycles[0] <= 0 or cycles[0] != cycles[1]:
        raise ValueError("simulator did not complete two repeatable runs")
    receipt = {
        "status": "passed",
        "generation_steps": generation_steps,
        "selected_tokens": selected,
        "cycles": cycles,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "elapsed_seconds": elapsed,
    }
    if bindings:
        receipt.update(bindings)
    (directory / "production-execution.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("/tmp/llm2fpga-tinystories-bram-production-sv"))
    parser.add_argument("--generation-steps", type=int, choices=(1, 2, 16), default=2)
    parser.add_argument("--model-package", type=Path,
                        help="exact TinyStories model package (use a pinned Nix store path for replay)")
    parser.add_argument("--model-snapshot", type=Path,
                        help="exact TinyStories HF snapshot (use a pinned Nix store path for replay)")
    return parser.parse_args(argv)


def load_model_bundle(args, capture):
    package = args.model_package if args.model_package is not None else capture.DEFAULT_PACKAGE
    snapshot = args.model_snapshot if args.model_snapshot is not None else capture.DEFAULT_MODEL
    return capture.exact_adapter.load_successor_exact_model(
        capture.DEFAULT_CONTRACT, package, snapshot, capture.DEFAULT_GENERATION)


def main():
    args = parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    artifact = production.generate_production_kernel(generation_steps=args.generation_steps)
    capture = base._load(ROOT / "TinyStories/capture_fixed_point_model_token_step_oracle.py", "production_execution_oracle")
    bundle = load_model_bundle(args, capture)
    for name, tensor in base.materialize_model_sources(artifact, bundle).items():
        (directory / ("source-" + name + ".bin")).write_bytes(tensor.numpy().astype("<i8").tobytes())
    (directory / "model.futil").write_text(artifact.futil)
    (directory / "provenance.json").write_text(json.dumps(artifact.provenance, indent=2) + "\n")
    install = Path(subprocess.check_output(["nix", "build", "--no-link", "--print-out-paths", ".#calyx"], cwd=ROOT, text=True).strip())
    library = prepare_production_library(install, directory / "calyx-library")
    subprocess.run([str(install / "bin/calyx"), str(directory / "model.futil"), "-l", str(library), "-d", "cell-share", "-b", "verilog", "--synthesis", "--nested", "-o", str(directory / "model.sv")], cwd=ROOT, check=True)
    model_path = directory / "model.sv"
    # Keep the experimental multiply repair opt-in until the Slang frontend
    # preserves its bounded operand widths during full-top elaboration.
    if os.environ.get("LLM2FPGA_APPLY_SIGNED_MULTIPLY_REPAIR") == "1":
        repaired_path = directory / "model.repaired.sv"
        repair_receipt = directory / "signed-multiply-width-repair.json"
        repair = base._load(ROOT / "scripts/pipeline/repair_calyx_signed_multiply_width.py", "signed_multiply_width_repair")
        repair.repair(model_path, repaired_path, repair_receipt)
        repaired_path.replace(model_path)
    model_path.write_text(annotate_bram_memories(model_path.read_text()))
    source_names = list(artifact.provenance["source_memories"])
    visibility = [
        '`verilator_config',
        'public_flat_rw -module "seq_mem_d1" -var "mem" @(posedge clk)',
        'public_flat_rw -module "seq_mem_d1_bram" -var "mem" @(posedge clk)',
    ]
    visibility += [f'public_flat_rd -module "main" -var "{name}"' for name in (
        "selected_tokens_write_en", "selected_tokens_content_en", "selected_tokens_addr0",
        "context_tokens_write_en", "context_tokens_content_en",
        "context_tokens_addr0", "context_tokens_write_data")]
    (directory / "visibility.vlt").write_text("\n".join(visibility) + "\n")
    (directory / "harness.cpp").write_text(harness(artifact))
    build = directory / "verilator"
    subprocess.run(["verilator", "--cc", "--exe", "--build", "-j", "8", "--top-module", "main", "--Mdir", str(build), "-CFLAGS", "-std=c++17 -O3", "-o", "production_harness", str(directory / "visibility.vlt"), str(directory / "model.sv"), str(directory / "harness.cpp")], cwd=ROOT, check=True)
    run_and_record_harness(
        build / "production_harness", directory, generation_steps=args.generation_steps,
        bindings={
            "oracle_sha256": artifact.provenance["oracle_sha256"],
            "futil_sha256": artifact.provenance["futil_sha256"],
            "sv_sha256": hashlib.sha256((directory / "model.sv").read_bytes()).hexdigest(),
            "memory_latency_receipt_sha256": json.loads(
                (library / "production-memory-latency.json").read_text())["receipt_sha256"],
        },
    )


if __name__ == "__main__":
    main()
