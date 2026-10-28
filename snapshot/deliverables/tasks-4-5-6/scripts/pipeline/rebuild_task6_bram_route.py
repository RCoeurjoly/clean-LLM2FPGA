#!/usr/bin/env python3
"""Route the source-rebuilt two-ROM Task 6 placement at the accepted profile."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def main() -> None:
    placement, nextpnr, chipdb, constraints, out = map(Path, sys.argv[1:])
    from scripts.pipeline.validate_tinystories_stage import validate_stage
    validate_stage("placement", placement, {})
    previous = json.loads((placement / "receipt.json").read_text())
    assert previous["status"] == "source-rebuilt-packed-placed-not-routed"
    assert previous["clock_mhz"] == 30 and previous["seed"] == 15
    assert hashlib.sha256(constraints.read_bytes()).hexdigest() == previous["constraints_sha256"]
    chipdb_file = chipdb if chipdb.is_file() else chipdb / "xc7k480tffg1156.bin"
    assert chipdb_file.is_file(), "missing Kintex-7 chip database"
    out.mkdir()
    command = [str(nextpnr / "bin/nextpnr-xilinx"), "--chipdb", str(chipdb_file),
               "--xdc", str(constraints), "--freq", "30", "--seed", "15", "--no-tmdriv",
               "--json", str(placement / "placed.json"), "--no-pack", "--no-place",
               "--write", str(out / "routed.json"), "--fasm", str(out / "routed.fasm")]
    start = time.monotonic()
    print("physical-stage route; deadline_seconds=5400", flush=True)
    with (out / "route.log").open("w") as log:
        subprocess.run(command, check=True, timeout=5400, stdout=log, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - start
    log = (out / "route.log").read_text()
    iterations = [{"iteration": int(index), "overuse": int(overuse),
                   "archfail": int(archfail) if archfail != "NA" else None}
                  for index, overuse, archfail in re.findall(
                      r"iter=(\d+).*?overuse=(\d+) archfail=(NA|\d+)", log)]
    assert iterations and iterations[-1]["overuse"] == iterations[-1]["archfail"] == 0
    timing = re.search(r"Max frequency for clock\s+'accelerator_clk':\s*([\d.]+) MHz \(PASS at 30[.]00 MHz\)", log)
    assert timing, "covered accelerator-clock paths did not pass 30 MHz"
    assert (out / "routed.json").stat().st_size > 0 and (out / "routed.fasm").stat().st_size > 0
    fasm_sha = hashlib.sha256((out / "routed.fasm").read_bytes()).hexdigest()
    (out / "receipt.json").write_text(json.dumps({
        "status": "source-rebuilt-routed-covered-paths-pass",
        "placement": str(placement), "clock_mhz": 30, "seed": 15,
        "constraints_sha256": previous["constraints_sha256"], "elapsed_seconds": elapsed,
        "deadline_seconds": 5400, "route_iterations": iterations,
        "covered_accelerator_clock_mhz": float(timing.group(1)),
        "fasm_sha256": fasm_sha,
        "limits": ["Covered-path timing only; BRAM/DSP internal timing is incompletely modeled.",
                   "Routing is not bitstream conversion or new board inference."],
    }, indent=2) + "\n")
    validate_stage("route", out, {})


if __name__ == "__main__":
    main()
