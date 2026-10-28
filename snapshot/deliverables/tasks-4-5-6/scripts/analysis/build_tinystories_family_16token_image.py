#!/usr/bin/env python3
"""Build the exact 1M sixteen-step image with the Task 5 family generator."""

from __future__ import annotations

import argparse
import filecmp
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from TinyStories.family_fixed_point_oracle import load_family_oracle
from scripts.pipeline.build_tinystories_family_image import write_image_words
from scripts.pipeline.fix_sv_synthesis_frontend import normalize
from scripts.pipeline.lower_fixed_point_model_to_calyx import (
    generate_model_kernel, materialize_model_sources,
)
from scripts.pipeline.prepare_tinystories_1m_bram_image import staticize_sv
from scripts.pipeline.tinystories_family_spec import FamilyModelSpec


EXPECTED_PROMPT = [7454, 2402, 257, 640]
EXPECTED_TOKENS = [11, 612, 373, 257, 1310, 2576, 3706, 20037,
                   13, 1375, 6151, 284, 711, 2354, 287, 262]


def build(snapshot: Path, package: Path, revision: str, oracle_path: Path,
          reference_image: Path, calyx: Path, calyx_library: Path,
          output: Path) -> dict:
    oracle = json.loads(oracle_path.read_text())
    if (oracle.get("size_label") != "1M" or oracle.get("revision") != revision
            or oracle.get("prompt_ids") != EXPECTED_PROMPT
            or oracle.get("tokens") != EXPECTED_TOKENS):
        raise ValueError("the pinned 1M sixteen-token oracle contract differs")
    spec = FamilyModelSpec.from_package(snapshot, package, revision)
    artifact = generate_model_kernel(oracle_path, model_spec=spec, production=True,
                                     generation_steps=16)
    if artifact.provenance["schedule"]["generation_steps"] != 16:
        raise ValueError("the family generator did not produce sixteen steps")
    output.mkdir(parents=True, exist_ok=True)
    (output / "model.futil").write_text(artifact.futil)
    compiled = subprocess.run(
        [str(calyx), str(output / "model.futil"), "-l", str(calyx_library),
         "-d", "cell-share", "-b", "verilog"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    if compiled.returncode:
        raise ValueError(f"Calyx failed ({compiled.returncode}): {compiled.stderr[-4000:]}")
    top = re.search(r"\bmodule main\s*\((.*?)\);", compiled.stdout, re.S)
    if top is None or not all(re.search(rf"\b{name}\b", top[1])
                              for name in ("clk", "reset", "start", "done", "token_id")):
        raise ValueError("Calyx output lacks the autoregressive main interface")
    (output / "model.sv").write_text(compiled.stdout)

    declarations = {
        name: {"width": int(record["width"]), "elements": int(record["elements"])}
        for name, record in artifact.provenance["source_memories"].items()
    }
    model = load_family_oracle(spec, package)
    tensors = materialize_model_sources(artifact, SimpleNamespace(model=model))
    records = write_image_words(output, tensors, declarations)
    reference = json.loads((reference_image / "image.json").read_text())
    if reference.get("source_memories") != records:
        raise ValueError("sixteen-step source geometry differs from the one-token image")
    for name in records:
        filename = f"image-{name}.hex"
        if not filecmp.cmp(output / filename, reference_image / filename, shallow=False):
            raise ValueError(f"sixteen-step model image differs at {name}")
    normalized, _ = normalize(staticize_sv(compiled.stdout, set(records)))
    (output / "model-static-init-normalized.sv").write_text(normalized)
    receipt = {
        "schema": "task5-tinystories-family-1m-sixteen-token-image-v1",
        "status": "image_emitted_not_simulated",
        "size_label": "1M", "revision": revision, "generation_steps": 16,
        "source_memories": records,
        "image_identical_to_first_token": True,
        "top_ports": ["clk", "reset", "start", "done", "token_id"],
    }
    (output / "image.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "package", "oracle", "reference-image", "calyx",
                 "calyx-library", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.snapshot, args.package, args.revision, args.oracle,
                           args.reference_image, args.calyx, args.calyx_library,
                           args.output), sort_keys=True))


if __name__ == "__main__":
    main()
