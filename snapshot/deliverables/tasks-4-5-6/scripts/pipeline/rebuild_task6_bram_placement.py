#!/usr/bin/env python3
"""Pack and place the source-rebuilt TinyStories-1M two-ROM netlist."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def main() -> None:
    synthesis, nextpnr, chipdb, constraints, out = map(Path, sys.argv[1:])
    from scripts.pipeline.validate_tinystories_stage import validate_stage
    validate_stage("synthesis", synthesis, {})
    source = json.loads((synthesis / "receipt.json").read_text())
    assert source["status"] == "source-rebuilt-accepted-two-rom-synthesis"
    model = json.loads((synthesis / "design.json").read_text())
    top = model["modules"]["tinystories_1m_ypcb_bram_production_top"]
    top.setdefault("settings", {})["kintexHeap/hpwlScaleY"] = "1"
    out.mkdir()
    configured = out / "configured.json"
    configured.write_text(json.dumps(model, separators=(",", ":")))
    del model, top
    chipdb_file = chipdb if chipdb.is_file() else chipdb / "xc7k480tffg1156.bin"
    assert chipdb_file.is_file(), "missing Kintex-7 chip database"
    common = [str(nextpnr / "bin/nextpnr-xilinx"), "--chipdb",
              str(chipdb_file), "--xdc", str(constraints),
              "--freq", "30", "--seed", "15", "--no-tmdriv"]
    stages = []

    def stage(name: str, arguments: list[str], deadline: int) -> None:
        start = time.monotonic()
        print("physical-stage " + name, flush=True)
        with (out / (name + ".log")).open("w") as log:
            subprocess.run(common + arguments, check=True, timeout=deadline,
                           stdout=log, stderr=subprocess.STDOUT)
        stages.append({"stage": name, "elapsed_seconds": time.monotonic() - start,
                       "deadline_seconds": deadline})

    stage("pack", ["--json", str(configured), "--pack-only", "--write", str(out / "packed.json")], 600)
    stage("place", ["--json", str(out / "packed.json"), "--no-pack", "--no-route",
                    "--write", str(out / "placed.json")], 1200)
    assert (out / "placed.json").stat().st_size > 0
    (out / "receipt.json").write_text(json.dumps({
        "status": "source-rebuilt-packed-placed-not-routed", "synthesis": str(synthesis),
        "structural_sha256": source["structural_sha256"], "clock_mhz": 30, "seed": 15,
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(), "stages": stages,
        "limits": ["No routing, bitstream, hardware or complete BRAM/DSP timing qualification."],
    }, indent=2) + "\n")
    validate_stage("placement", out, {})


if __name__ == "__main__":
    main()
