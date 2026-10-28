#!/usr/bin/env python3
"""Run a wide-model RTL gate with bounded runtime and live checkpoint progress."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event, Thread

from scripts.pipeline import simulate_tinystories_family_first_token as simulation


def run_with_monitor(image: Path, oracle: Path, output: Path, *, max_cycles: int,
                     run_timeout: int, poll_seconds: float = 30.0) -> dict[str, object]:
    if max_cycles <= 0 or run_timeout <= 0 or poll_seconds <= 0:
        raise ValueError("simulation cycle, time and poll bounds must be positive")
    stop = Event()
    progress = output / "progress.jsonl"
    seen = [0]

    def drain() -> None:
        if not progress.exists():
            return
        lines = progress.read_text().splitlines()
        for line in lines[seen[0]:]:
            print("RTL-progress " + line, flush=True)
        seen[0] = len(lines)

    def monitor() -> None:
        while not stop.wait(poll_seconds):
            drain()

    thread = Thread(target=monitor, daemon=True)
    thread.start()
    try:
        result = simulation.run(image, oracle, output, max_cycles=max_cycles,
                                run_timeout=run_timeout)
        print("RTL-result " + json.dumps(result, sort_keys=True), flush=True)
        return result
    finally:
        stop.set()
        thread.join()
        drain()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "oracle", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--max-cycles", type=int, required=True)
    parser.add_argument("--run-timeout", type=int, required=True)
    args = parser.parse_args()
    run_with_monitor(args.image, args.oracle, args.output,
                     max_cycles=args.max_cycles, run_timeout=args.run_timeout)


if __name__ == "__main__":
    main()
