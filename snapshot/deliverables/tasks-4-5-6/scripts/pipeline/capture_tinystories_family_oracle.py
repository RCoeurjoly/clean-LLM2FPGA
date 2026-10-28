#!/usr/bin/env python3
"""Capture Nix-pinned fixed-point token/checkpoint references for Task 5."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import torch

from scripts.pipeline.tinystories_family_spec import FamilyModelSpec
from TinyStories.family_fixed_point_oracle import compare_logits, generate_tokens, load_family_oracle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--size", required=True)
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--expected-generation", type=Path,
                        help="accepted 1M generation artifact, used as a semantic regression gate")
    parser.add_argument("--expected-logits", type=Path,
                        help="accepted 1M full first-token Q16.16 logits vector")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = FamilyModelSpec.from_package(args.snapshot, args.package, args.revision)
    model = load_family_oracle(spec, args.package)
    result = generate_tokens(model, spec.frozen_prompt_ids, steps=args.steps)
    if args.expected_generation:
        expected = json.loads(args.expected_generation.read_text())["generation"]["expected_tokens"]
        if result["tokens"] != expected:
            first = next((index for index, pair in enumerate(zip(result["tokens"], expected))
                          if pair[0] != pair[1]), min(len(result["tokens"]), len(expected)))
            raise ValueError(f"{args.size}: frozen generation diverges at token {first}: "
                             f"observed={result['tokens']} expected={expected}")
    if args.expected_logits:
        expected_logits = json.loads(args.expected_logits.read_text())["logits"]["values"]
        with torch.no_grad():
            logits, _ = model._execute(torch.tensor([list(spec.frozen_prompt_ids)], dtype=torch.int64))
        compare_logits(logits[0, -1].tolist(), expected_logits)
    receipt = {
        "schema": "task5-tinystories-fixed-point-family-oracle-v1",
        "size_label": args.size,
        "revision": spec.revision,
        "tokenizer_identity": spec.tokenizer_identity,
        "layers": spec.layers,
        "hidden": spec.hidden,
        "numeric_contract": "W8A8-Q16.16-Q8.24-signed64-ascending-wrapping-MAC",
        **result,
    }
    args.output.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
