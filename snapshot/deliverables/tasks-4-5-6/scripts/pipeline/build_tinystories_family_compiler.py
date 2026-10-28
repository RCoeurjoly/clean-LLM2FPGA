#!/usr/bin/env python3
"""Build one pinned TinyStories family model with an explicit Calyx tool input.

Nix provides the model, oracle and compiler; this stage never invokes Nix or
records host timing/path data inside its reproducible output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from scripts.pipeline.lower_fixed_point_model_to_calyx import generate_model_kernel
from scripts.pipeline.tinystories_family_spec import FamilyModelSpec


def build(snapshot: Path, package: Path, revision: str, oracle: Path,
          calyx: Path, calyx_library: Path, output: Path, *, size: str,
          timeout: int = 1800) -> dict[str, object]:
    if not 0 < timeout <= 1800:
        raise ValueError("Calyx time bound must be between 1 and 1800 seconds")
    from scripts.comparison.verify_closure_reference import validate_narrowing_bounds
    bounds = validate_narrowing_bounds(package)
    if not bounds["signed_64_bit_product_valid"]:
        raise ValueError("family post-scale product exceeds supported signed 64-bit contract")
    spec = FamilyModelSpec.from_package(snapshot, package, revision)
    artifact = generate_model_kernel(oracle, model_spec=spec, production=True,
                                     generation_steps=1)
    output.mkdir(parents=True, exist_ok=True)
    (output / "model.futil").write_text(artifact.futil)
    command = [str(calyx), str(output / "model.futil"), "-l", str(calyx_library),
               "-d", "cell-share", "-b", "verilog"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                            check=False)
    if result.returncode:
        raise ValueError(f"Calyx failed ({result.returncode}): {result.stderr[-4000:]}")
    top = re.search(r"\bmodule main\s*\((.*?)\);", result.stdout, re.S)
    if top is None or not all(re.search(rf"\b{name}\b", top[1])
                              for name in ("clk", "reset", "start", "done")):
        raise ValueError("Calyx output lacks the main controller interface")
    (output / "model.sv").write_text(result.stdout)
    source_memories = {
        name: {"elements": int(record["elements"]), "width": int(record["width"])}
        for name, record in sorted(artifact.provenance["source_memories"].items())
    }
    receipt: dict[str, object] = {
        "schema": "task5-tinystories-family-generated-sv-v1",
        "size_label": size, "revision": revision,
        "layers": spec.layers, "hidden": spec.hidden, "vocab": spec.vocab,
        "source_memories": source_memories,
        "requantization_width": 64,
        "narrowing_bounds": bounds,
        "top_ports": ["clk", "reset", "start", "done"],
        "futil_bytes": len(artifact.futil.encode()),
        "sv_bytes": len(result.stdout.encode()),
    }
    (output / "build.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "package", "oracle", "calyx", "calyx-library", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--size", required=True)
    args = parser.parse_args()
    build(args.snapshot, args.package, args.revision, args.oracle, args.calyx,
          args.calyx_library, args.output, size=args.size)


if __name__ == "__main__":
    main()
