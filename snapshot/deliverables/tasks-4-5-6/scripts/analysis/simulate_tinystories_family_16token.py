#!/usr/bin/env python3
"""Run the generated TinyStories-1M family image for autonomous tokens."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

from scripts.pipeline.run_bram_production_sv import harness


VISIBILITY = '''`verilator_config
public_flat_rw -module "seq_mem_d1" -var "mem" @(posedge clk)
public_flat_rw -module "seq_mem_d1_bram" -var "mem" @(posedge clk)
public_flat_rd -module "main" -var "selected_tokens_write_en"
public_flat_rd -module "main" -var "selected_tokens_content_en"
public_flat_rd -module "main" -var "selected_tokens_addr0"
public_flat_rd -module "main" -var "context_tokens_write_en"
public_flat_rd -module "main" -var "context_tokens_content_en"
public_flat_rd -module "main" -var "context_tokens_addr0"
public_flat_rd -module "main" -var "context_tokens_write_data"
'''


def validate_events(lines: list[str], expected: list[int]) -> dict:
    if not expected or any(type(token) is not int for token in expected):
        raise ValueError("invalid frozen token reference")
    selected: list[list[int]] = [[], []]
    outputs: list[list[int]] = [[], []]
    cycles: list[int | None] = [None, None]
    started: set[int] = set()
    for line in lines:
        event = json.loads(line)
        kind = event.get("event")
        if kind == "context_mismatch":
            raise ValueError(f"RTL context mismatch: {event}")
        if kind not in ("started", "selected", "token_port", "complete"):
            continue
        run = event.get("run")
        if type(run) is not int or run not in (0, 1):
            raise ValueError("invalid RTL run index")
        if kind == "started":
            if run in started:
                raise ValueError("repeated RTL run start")
            started.add(run)
        elif run not in started:
            raise ValueError("RTL event before run start")
        elif kind in ("selected", "token_port"):
            target = selected if kind == "selected" else outputs
            if event.get("step") != len(target[run]) or type(event.get("token")) is not int:
                raise ValueError("missing or unordered RTL token event")
            target[run].append(event["token"])
        elif cycles[run] is not None or type(event.get("cycles")) is not int:
            raise ValueError("missing or repeated RTL completion cycle")
        else:
            cycles[run] = event["cycles"]
    if started != {0, 1} or any(cycle is None for cycle in cycles):
        raise ValueError("RTL did not finish two complete runs")
    if selected != [expected, expected]:
        raise ValueError(f"RTL selected tokens differ from reference: {selected}")
    if outputs != [expected, expected]:
        raise ValueError(f"RTL output tokens differ from reference: {outputs}")
    if cycles[0] <= 0 or cycles[0] != cycles[1]:
        raise ValueError("RTL runs have missing or inconsistent cycles")
    return {
        "schema": "task5-tinystories-family-autoregressive-simulation-v1",
        "status": "matched", "generation_steps": len(expected),
        "selected_tokens": selected, "output_tokens": outputs,
        "cycles": cycles,
    }


def run(image: Path, oracle_path: Path, output: Path) -> dict:
    image_receipt = json.loads((image / "image.json").read_text())
    oracle = json.loads(oracle_path.read_text())
    expected = oracle["tokens"]
    if (image_receipt.get("size_label") != "1M"
            or image_receipt.get("generation_steps") != 16
            or image_receipt.get("image_identical_to_first_token") is not True
            or oracle.get("size_label") != "1M" or len(expected) != 16):
        raise ValueError("simulation requires the exact family 1M sixteen-token image")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="task5-family-16token-") as temporary:
        scratch = Path(temporary)
        source = scratch / "harness.cpp"
        source.write_text(harness(SimpleNamespace(provenance={
            "schedule": {"generation_steps": 16}, "source_memories": {},
        })))
        visibility = scratch / "visibility.vlt"
        visibility.write_text(VISIBILITY)
        build = scratch / "verilator"
        command = [
            "verilator", "--cc", "--exe", "--build", "-j", "2",
            "--top-module", "main", "--Mdir", str(build),
            "-CFLAGS", "-std=c++17 -O3", "-o", "family16_harness",
            str(visibility), str(image / "model-static-init-normalized.sv"), str(source),
        ]
        compiled = subprocess.run(command, capture_output=True, text=True,
                                  timeout=1800, check=False)
        if compiled.returncode:
            raise ValueError(f"Verilator build failed: {compiled.stderr[-4000:]}")
        trace_path = output / "execution.jsonl"
        with trace_path.open("w") as trace:
            process = subprocess.Popen([str(build / "family16_harness"), str(image)],
                                       cwd=image, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            assert process.stdout is not None
            for line in process.stdout:
                trace.write(line)
                if '"event":"progress"' in line and '"cycles":' in line:
                    event = json.loads(line)
                    if event["cycles"] % 100_000_000 == 0:
                        print(line.rstrip(), flush=True)
            status = process.wait()
        if status:
            raise ValueError(f"sixteen-token RTL simulator exited {status}; "
                             f"last events: {trace_path.read_text().splitlines()[-5:]}")
    receipt = validate_events((output / "execution.jsonl").read_text().splitlines(), expected)
    (output / "result.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "oracle", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.image, args.oracle, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
