#!/usr/bin/env python3
"""Emit a validated model-specific family specification from pinned Nix inputs."""

import argparse
from dataclasses import fields
import json
from pathlib import Path

from scripts.pipeline.tinystories_family_spec import FamilyModelSpec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = FamilyModelSpec.from_package(args.snapshot, args.package, args.revision)
    spec.validate_prompt((7454, 2402, 257, 640))
    data = {field.name: getattr(spec, field.name) for field in fields(spec)}
    data["source_shapes"] = dict(spec.source_shapes)
    args.output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
