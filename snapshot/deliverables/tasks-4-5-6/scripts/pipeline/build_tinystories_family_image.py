#!/usr/bin/env python3
"""Emit synthesis-visible family source memories from the compiler's plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import torch

from TinyStories.family_fixed_point_oracle import load_family_oracle
from scripts.pipeline.fix_sv_synthesis_frontend import normalize
from scripts.pipeline.lower_fixed_point_model_to_calyx import (
    generate_model_kernel, materialize_model_sources,
)
from scripts.pipeline.prepare_tinystories_1m_bram_image import staticize_sv
from scripts.pipeline.tinystories_family_spec import FamilyModelSpec


def write_image_words(output: Path, tensors: dict, declarations: dict) -> dict[str, dict[str, int]]:
    if set(tensors) != set(declarations):
        raise ValueError("source memory names differ from compiler declarations")
    output.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, int]] = {}
    for name, record in sorted(declarations.items()):
        width, elements = int(record["width"]), int(record["elements"])
        tensor = tensors[name].detach().cpu().to(dtype=torch.int64).flatten()
        if width < 1 or width > 64 or tensor.numel() != elements:
            raise ValueError(f"{name}: source memory shape/width differs from declaration")
        digits, mask = (width + 3) // 4, (1 << width) - 1
        path = output / f"image-{name}.hex"
        with path.open("w") as stream:
            for start in range(0, elements, 8192):
                chunk = tensor[start:start + 8192].tolist()
                if any(value < -(1 << (width - 1)) or value > mask for value in chunk):
                    raise ValueError(f"{name}: source word outside declared width")
                stream.write("".join(f"{value & mask:0{digits}x}\n" for value in chunk))
        records[name] = {"width": width, "elements": elements}
    return records


def build(snapshot: Path, package: Path, revision: str, oracle: Path,
          compiler: Path, output: Path, *, size: str) -> dict[str, object]:
    spec = FamilyModelSpec.from_package(snapshot, package, revision)
    artifact = generate_model_kernel(oracle, model_spec=spec, production=True,
                                     generation_steps=1)
    if artifact.futil != (compiler / "model.futil").read_text():
        raise ValueError("compiler/image Futil plans differ")
    compiled = json.loads((compiler / "build.json").read_text())
    declarations = {
        name: {"width": record["width"], "elements": record["elements"]}
        for name, record in artifact.provenance["source_memories"].items()
    }
    if compiled["source_memories"] != declarations:
        raise ValueError("compiler/image source declarations differ")
    model = load_family_oracle(spec, package)
    tensors = materialize_model_sources(artifact, SimpleNamespace(model=model))
    records = write_image_words(output, tensors, declarations)
    static = staticize_sv((compiler / "model.sv").read_text(), set(tensors))
    normalized, _ = normalize(static)
    (output / "model-static-init-normalized.sv").write_text(normalized)
    receipt: dict[str, object] = {
        "schema": "task5-tinystories-family-static-image-v1",
        "status": "image_emitted_not_simulated",
        "size_label": size, "revision": revision,
        "source_memories": records,
        "source_word_count": sum(record["elements"] for record in records.values()),
    }
    (output / "image.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "package", "oracle", "compiler", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--size", required=True)
    args = parser.parse_args()
    build(args.snapshot, args.package, args.revision, args.oracle,
          args.compiler, args.output, size=args.size)


if __name__ == "__main__":
    main()
