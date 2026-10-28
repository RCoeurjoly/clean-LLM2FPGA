#!/usr/bin/env python3
"""Rebuild the accepted full16 compiler SV and its isolated c_proj ROM variant."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


def sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def primitive_body(source: str, name: str) -> str:
    start = source.index(f"module {name} #")
    end = source.index("endmodule", start) + len("endmodule")
    return (source[start:end]
            .replace(name, "seq_mem_d1_variant", 1)
            .replace('  (* ram_style = "block" *)\n', "")
            .replace('  (* ram_style = "distributed" *)\n', ""))


def main() -> None:
    repo, calyx, out = map(Path, sys.argv[1:])
    sys.path.insert(0, str(repo))
    from scripts.pipeline import run_bram_production_sv as runner
    from scripts.pipeline.validate_tinystories_stage import validate_stage

    artifact = runner.production.generate_production_kernel(generation_steps=16)
    out.mkdir()
    (out / "model.futil").write_text(artifact.futil)
    library = runner.prepare_production_library(calyx, Path("calyx-library").resolve())
    raw = out / "compiler-raw.sv"
    command = [str(calyx / "bin/calyx"), str(out / "model.futil"),
               "-l", str(library), "-d", "cell-share", "-b", "verilog",
               "--synthesis", "--nested", "-o", str(raw)]
    with (out / "calyx.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600, check=True)
    control = runner.annotate_bram_memories(raw.read_text())
    runner.LUTRAM_MEMORIES = runner.LUTRAM_MEMORIES - {"c_proj_weight_codes_i8"}
    variant = runner.annotate_bram_memories(raw.read_text())
    instance = control.index(" c_proj_weight_codes_i8 (")
    start = control.rfind("seq_mem_d1_lutram # (", 0, instance)
    assert start >= 0, "missing c_proj LUTRAM instance"
    expected = control[:start] + "seq_mem_d1_bram" + control[start + len("seq_mem_d1_lutram"):]
    assert variant == expected, "two-ROM change exceeds the c_proj instance mapping"
    assert primitive_body(control, "seq_mem_d1_lutram") == primitive_body(control, "seq_mem_d1_bram")
    (out / "control.sv").write_text(control)
    (out / "model.sv").write_text(variant)
    (out / "generator-provenance.json").write_text(json.dumps(artifact.provenance, indent=2) + "\n")
    receipt = {
        "status": "source-rebuilt-accepted-two-rom-compiler-sv",
        "compiler_ir_sha256": sha256(artifact.futil),
        "control_sv_sha256": sha256(control),
        "variant_sv_sha256": sha256(variant),
        "changed_memory": "c_proj_weight_codes_i8",
        "primitive_behavior_identical": True,
        "limits": ["Compiler/Calyx stage only; model image, synthesis, route, bitstream and board not rebuilt."],
    }
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    validate_stage("calyx", out, {})


if __name__ == "__main__":
    main()
