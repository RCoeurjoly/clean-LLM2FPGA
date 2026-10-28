#!/usr/bin/env python3
"""Bounded XC7 synthesis of a simulation-accepted compiler family image."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time


def count_resources(design: dict) -> dict[str, int]:
    cells = design["modules"]["main"]["cells"]
    types = Counter(cell["type"] for cell in cells.values())
    return {
        "LUT": sum(types[f"LUT{width}"] for width in range(1, 7)),
        "FF": sum(types[name] for name in (
            "FDRE", "FDSE", "FDCE", "FDPE", "FDRE_1", "FDSE_1", "FDCE_1", "FDPE_1")),
        "BRAM36": types["RAMB36E1"],
        "BRAM18": types["RAMB18E1"],
        "DSP": types["DSP48E1"],
    }


YOSYS_PASS = re.compile(r"^\s*\d+(?:\.\d+)*\.\s+\S")


def _scan_yosys_log(log: Path, offset: int, previous_pass: str | None,
                    previous_line: str | None) -> tuple[int, str | None, str | None]:
    if not log.exists():
        return offset, previous_pass, previous_line
    with log.open(errors="replace") as stream:
        stream.seek(offset if log.stat().st_size >= offset else 0)
        while line := stream.readline():
            if line.strip():
                previous_line = line.strip()[:300]
            if YOSYS_PASS.match(line):
                previous_pass = line.strip()[:300]
        return stream.tell(), previous_pass, previous_line


def _process_usage(pid: int) -> tuple[int | None, float | None]:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None, None
    rss = next((int(line.split()[1]) for line in status.splitlines()
                if line.startswith("VmRSS:")), None)
    cpu = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
    return rss, round(cpu, 1)


def run_monitored_command(command: list[str], cwd: Path, log_path: Path,
                          *, timeout_seconds: float, progress_interval_seconds: float,
                          size: str | None, min_free_bytes: int = 0) -> None:
    if timeout_seconds <= 0 or progress_interval_seconds <= 0:
        raise ValueError("monitor limits must be positive")
    started = time.monotonic()
    last_pass = None
    last_line = None
    log_offset = 0
    last_pass_changed = started
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=cwd, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    process.wait(timeout=min(progress_interval_seconds, remaining))
                except subprocess.TimeoutExpired:
                    pass
                log_offset, observed_pass, last_line = _scan_yosys_log(
                    log_path, log_offset, last_pass, last_line)
                if observed_pass != last_pass:
                    last_pass = observed_pass
                    last_pass_changed = time.monotonic()
                rss_kib, cpu_seconds = _process_usage(process.pid)
                disk_free = shutil.disk_usage(cwd).free
                print(json.dumps({
                    "event": "synthesis_progress", "size_label": size,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "seconds_since_pass_change": round(time.monotonic() - last_pass_changed, 1),
                    "last_pass": last_pass, "last_line": last_line,
                    "log_bytes": log_offset,
                    "rss_kib": rss_kib, "cpu_seconds": cpu_seconds,
                    "disk_free_gib": round(disk_free / 2**30, 2),
                    "running": process.poll() is None,
                }), flush=True)
                if disk_free < min_free_bytes:
                    raise OSError(f"disk reserve breached: {disk_free} bytes free, "
                                  f"required {min_free_bytes}")
                if process.poll() is not None:
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, command)
                    return
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def run_yosys(yosys: Path, slang: Path, output: Path, timeout_seconds: int,
              size: str | None = None) -> None:
    command = (
        "read_slang --threads 1 --no-proc --max-parse-depth 20000 --top main "
        "model-static-init-normalized.sv; hierarchy -top main -check; proc; "
        "check; tribuf -logic; deminout; opt_expr; opt_clean; "
        "scratchpad -set xilinx_dsp.multonly 1; "
        "synth_xilinx -family xc7 -top main -flatten -noiopad -noclkbuf; "
        "check; stat; write_json design.json"
    )
    run_monitored_command([str(yosys / "bin/yosys"), "-m",
                           str(slang / "share/yosys/plugins/slang.so"), "-p", command],
                          output, output / "yosys.log", timeout_seconds=timeout_seconds,
                          progress_interval_seconds=60, size=size,
                          min_free_bytes=0 if size == "1M" else 10 * 2**30)


def first_yosys_error(log: Path) -> str | None:
    if not log.exists():
        return None
    with log.open(errors="replace") as stream:
        for line in stream:
            if "ERROR:" in line:
                return line.strip()[:1000]
    return None


def build(image: Path, simulation: Path, output: Path, *, size: str,
          yosys: Path, slang: Path, timeout_seconds: int) -> dict:
    image_receipt = json.loads((image / "image.json").read_text())
    if image_receipt.get("size_label") != size:
        raise ValueError("compiler image size differs from synthesis size")
    sim_receipt = json.loads((simulation / "result.json").read_text())
    if sim_receipt.get("status") != "matched":
        raise ValueError("first-token simulation has not passed")
    if timeout_seconds <= 0:
        raise ValueError("synthesis timeout must be positive")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image / "model-static-init-normalized.sv",
                    output / "model-static-init-normalized.sv")
    for source in image.glob("image-*.hex"):
        shutil.copyfile(source, output / source.name)
    started = time.monotonic()
    status, reason, resources = "pass", None, None
    try:
        run_yosys(yosys, slang, output, timeout_seconds, size)
        design = json.loads((output / "design.json").read_text())
        resources = count_resources(design)
    except (subprocess.TimeoutExpired, TimeoutError):
        status, reason = "timeout", f"Yosys exceeded {timeout_seconds} seconds"
    except subprocess.CalledProcessError as error:
        status = "failed"
        detail = first_yosys_error(output / "yosys.log")
        reason = f"Yosys exited {error.returncode}: {detail or 'no ERROR line in log'}"
    except (OSError, ValueError, KeyError) as error:
        status, reason = "failed", f"{type(error).__name__}: {error}"
    if status != "pass":
        (output / "design.json").unlink(missing_ok=True)
    # The full Yosys log contains wall-clock timings and host memory readings.
    # Keep diagnostics in the deterministic receipt, not in a Nix output file.
    (output / "yosys.log").unlink(missing_ok=True)
    print(json.dumps({"event": "synthesis_timing", "size_label": size,
                      "elapsed_seconds": time.monotonic() - started}), flush=True)
    receipt = {
        "schema": "task5-tinystories-family-synthesis-v1",
        "size_label": size, "status": status, "reason": reason,
        "resources": resources,
        "timeout_seconds": timeout_seconds,
        "scope": "XC7 synthesis only; no fit, placement, route, timing or board claim",
    }
    (output / "result.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "simulation", "output", "yosys", "slang"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--size", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    print(json.dumps(build(args.image, args.simulation, args.output, size=args.size,
                           yosys=args.yosys, slang=args.slang,
                           timeout_seconds=args.timeout_seconds)), flush=True)


if __name__ == "__main__":
    main()
