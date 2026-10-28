#!/usr/bin/env python3
"""Run the bounded native XC7 synthesis of the one-ROM ablation baseline image."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


EXPECTED_RESOURCES = {"RAMB36E1": 784, "RAMB18E1": 89, "RAM64M": 15150, "DSP48E1": 190}


def strip_source_locations(value):
    """Ignore only Yosys source locations, including two path-derived CSR net names."""
    if isinstance(value, dict):
        return {
            re.sub(r"/nix/store/[^/]+/(?:rtl/)?(?=tinystories_1m_jtag_csr\.sv)",
                   "/source/", key): strip_source_locations(item)
            for key, item in value.items() if key != "src"
        }
    if isinstance(value, list):
        return [strip_source_locations(item) for item in value]
    return value


def main() -> None:
    repo, image, yosys, slang, mapper_wrapper, out = map(Path, sys.argv[1:])
    manifest = json.loads((image / "manifest.json").read_text())
    assert manifest["status"] == "source-rebuilt-one-rom-baseline-static-image"
    assert len(manifest["source_memories"]) == 89
    out.mkdir()
    for path in image.glob("image-*.hex"):
        shutil.copyfile(path, path.name)
    shutil.copyfile(image / "model-static-init-normalized.sv", "model-static-init-normalized.sv")
    top = "tinystories_1m_ypcb_bram_production_top"
    policy = "scratchpad -set xilinx_dsp.multonly 1; "
    stages = []

    def stage(name: str, command: str, deadline: int = 600) -> None:
        start = time.monotonic()
        print("synthesis-stage " + name, flush=True)
        with (out / (name + ".log")).open("w") as log:
            subprocess.run([str(yosys / "bin/yosys"), "-m",
                            str(slang / "share/yosys/plugins/slang.so"), "-p", command],
                           timeout=deadline, stdout=log, stderr=subprocess.STDOUT, check=True)
        stages.append({"stage": name, "elapsed_seconds": time.monotonic() - start,
                       "deadline_seconds": deadline})

    stage("pre-dsp-core",
          "read_slang --threads 1 --no-proc --max-parse-depth 20000 --top main model-static-init-normalized.sv; "
          "hierarchy -top main -check; proc; check; tribuf -logic; deminout; opt_expr; opt_clean; "
          "opt -nodffe -nosdff; opt; wreduce; peepopt; opt_clean; sort; memory_dff; opt_expr -fine; "
          "wreduce; stat; write_rtlil pre-dsp-core.il")
    stage("dsp-core",
          "read_verilog -lib -specify +/xilinx/cells_sim.v; read_verilog -lib +/xilinx/cells_xtra.v; "
          "read_rtlil pre-dsp-core.il; " + policy +
          "synth_xilinx -family xc7 -top main -run map_dsp:coarse; stat; write_rtlil dsp-core.il")
    wrapper = " ".join(str(repo / "rtl" / name) for name in (
        "tinystories_1m_ypcb_clock.sv", "tinystories_1m_jtag_csr.sv",
        "tinystories_1m_ypcb_bram_production_top.sv"))
    stage("integrated-coarse",
          "read_rtlil dsp-core.il; read_verilog -sv " + wrapper + "; "
          "chparam -set TOKEN_INDEX_WIDTH 4 " + top + "; hierarchy -top " + top + " -check; "
          "proc; opt_expr; opt_clean; memory_dff; alumacc; share; opt; memory -nomap; opt_clean; "
          "stat; write_rtlil integrated-coarse.il")
    stage("memory-unpack", "read_rtlil integrated-coarse.il; memory_unpack; stat; write_rtlil unpacked.il")
    start = time.monotonic()
    with (out / "memory-map.log").open("w") as log:
        subprocess.run([sys.executable, str(mapper_wrapper),
                        "unpacked.il", "memory-checkpoints", "--yosys", str(yosys / "bin/yosys"),
                        "--top", "main", "--timeout", "120"],
                       check=True, timeout=7200, stdout=log, stderr=subprocess.STDOUT)
    mapped = json.loads(Path("memory-checkpoints/memory-checkpoints.json").read_text())
    assert mapped["status"] == "complete" and mapped["completed"] == mapped["memory_count"]
    stages.append({"stage": "memory-libmap", "elapsed_seconds": time.monotonic() - start,
                   "deadline_seconds": 7200})
    (out / "memory-map-receipt.json").write_text(json.dumps(mapped, indent=2) + "\n")
    stage("native-lutram", "read_rtlil " + mapped["last_checkpoint"] +
          "; techmap -map +/xilinx/lutrams_xc5v_map.v; write_rtlil native-lutram.il", 900)
    stage("native-bram", "read_rtlil native-lutram.il; techmap -map +/xilinx/brams_xc6v_map.v; "
          "stat; write_rtlil native-memory.il", 1200)
    stage("post-native-coarse", "read_rtlil native-memory.il; " + policy +
          "synth_xilinx -family xc7 -top " + top +
          " -run begin:coarse; stat; write_rtlil post-native-coarse.il", 900)
    stage("post-native-fine", "read_rtlil post-native-coarse.il; " + policy +
          "synth_xilinx -family xc7 -top " + top +
          " -run map_ffram:map_ffs; stat; write_rtlil post-native-fine.il", 900)
    stage("final", "read_rtlil post-native-fine.il; read_verilog -overwrite -lib -specify +/xilinx/cells_sim.v; "
          "read_verilog -overwrite -lib +/xilinx/cells_xtra.v; " + policy +
          "synth_xilinx -family xc7 -top " + top + " -run map_ffs: -json " +
          str(out / "design.json") + "; check; stat; write_rtlil " + str(out / "design.il"), 900)
    digest = hashlib.sha256((out / "design.json").read_bytes()).hexdigest()
    netlist = json.loads((out / "design.json").read_text())
    structural = hashlib.sha256(json.dumps(strip_source_locations(netlist),
                                           sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    from collections import Counter
    resources = dict(Counter(cell["type"] for cell in netlist["modules"]["main"]["cells"].values()))
    assert all(resources.get(cell) == count for cell, count in EXPECTED_RESOURCES.items()), "one-ROM resource baseline changed"
    (out / "receipt.json").write_text(json.dumps({
        "status": "source-rebuilt-one-rom-baseline-synthesis", "top": top,
        "design_sha256": digest, "structural_sha256": structural,
        "image_manifest": manifest, "resources": resources, "stages": stages,
        "limits": ["Synthesis only; placement, route, bitstream and board not rebuilt."],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
