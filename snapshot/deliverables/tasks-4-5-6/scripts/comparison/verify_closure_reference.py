#!/usr/bin/env python3
"""Portable required CPU gate; no implicit personal caches or skipped inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
REF = ROOT / "artifacts/reference"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def authenticate_package(package_dir: Path) -> dict:
    contract = json.loads((REF / "tinystories-1m-exact-input-contract.json").read_text())
    expected = contract["package"]["files"]
    if not package_dir.is_dir():
        raise ValueError("package directory is missing")
    if {p.name for p in package_dir.iterdir() if p.is_file()} != set(expected) | {"receipt.json"}:
        raise ValueError("package file set differs from pinned contract")
    for name, identity in expected.items():
        path = package_dir / name
        if path.stat().st_size != identity["size"] or digest(path) != identity["sha256"]:
            raise ValueError(f"package identity mismatch: {name}")
    provenance = json.loads((REF / "tinystories-1m-export-provenance.json").read_text())
    if digest(package_dir / "receipt.json") != provenance["expected"]["package_receipt"]:
        raise ValueError("package receipt differs from pinned contract")
    return {name: identity["sha256"] for name, identity in expected.items()}


def _scale_code(value: float) -> int:
    import math
    if not math.isfinite(value):
        raise ValueError("nonfinite scale")
    code = round(value * (1 << 24))
    if not 0 < code < (1 << 24):
        raise ValueError("scale is outside unsigned Q8.24 contract")
    return code


def validate_narrowing_bounds(package_dir: Path) -> dict:
    """Conservative all-INT8-input GEMV bounds; Python integers do not wrap.

    For output row r: |acc| <= sum_i 128 * input_scale_i * |weight_ri|.
    Multiply that bound by the row's weight scale for post-rescale width.
    This is a package certificate, not an observed-prompt range estimate.
    """
    manifest = json.loads((package_dir / "manifest.json").read_text())
    weights = (package_dir / "weights.bin").read_bytes()
    scales = (package_dir / "scales.bin").read_bytes()
    rows = {}
    for name, tensor in manifest["tensors"].items():
        if tensor.get("format") != "symmetric_int8_per_output":
            continue
        if name.startswith("position_embedding"):
            continue  # lookup only, not a GEMV matrix
        if name == "token_embedding.weight":
            activation_name = "lm_head.input"
        else:
            parts = name.split(".")
            suffix = ".".join(parts[2:-1])
            module = {"attn.q": "attn.attention.q_proj", "attn.k": "attn.attention.k_proj",
                      "attn.v": "attn.attention.v_proj", "attn.out": "attn.attention.out_proj",
                      "mlp.fc": "mlp.c_fc", "mlp.proj": "mlp.c_proj"}[suffix]
            activation_name = f"transformer.h.{parts[1]}.{module}.input"
        output_count, width = tensor["logical_shape"]
        input_scales = [_scale_code(v) for v in manifest["activation_scales"][activation_name]]
        if len(input_scales) != width:
            raise ValueError(f"{name}: input scale geometry mismatch")
        offset = tensor["offset"]
        payload = weights[offset:offset + tensor["nbytes"]]
        if len(payload) != output_count * width:
            raise ValueError(f"{name}: truncated weight image")
        scale_offset = tensor["scale_offset"]
        scale_bytes = scales[scale_offset:scale_offset + tensor["scale_nbytes"]]
        if len(scale_bytes) != output_count * 4:
            raise ValueError(f"{name}: truncated scale image")
        row_scales = [_scale_code(v[0]) for v in struct.iter_unpack("<f", scale_bytes)]
        maximum = 0
        for row in range(output_count):
            accumulator = sum(128 * scale * abs(code if code < 128 else code - 256)
                              for scale, code in zip(input_scales, payload[row * width:(row + 1) * width]))
            maximum = max(maximum, accumulator * row_scales[row])
        rows[name] = maximum
    if not rows:
        raise ValueError("package has no GEMV tensors")
    bound = max(rows.values())
    package_digest = hashlib.sha256("".join(digest(package_dir / n) for n in
                                           ("manifest.json", "weights.bin", "scales.bin")).encode()).hexdigest()
    return {"package_digest": package_digest, "maximum_absolute_product_bound": bound,
            "signed_53_bit_product_valid": bound < (1 << 52),
            "signed_64_bit_product_valid": bound < (1 << 63), "per_tensor_bounds": rows,
            "method": "all_int8_codes_absolute_row_sum"}


def _pinned_numpy_reference(source_dir: Path):
    pinned = json.loads((REF / "tinystories-1m-fixed-logits-oracle.json").read_text())["identity"]["pinned_sources"]
    for name, identity in pinned.items():
        if not (source_dir / name).is_file() or digest(source_dir / name) != identity["sha256"]:
            raise ValueError(f"independent reference source mismatch: {name}")
    package_name = "_llm2fpga_pinned_numpy"
    spec = importlib.util.spec_from_file_location(package_name, source_dir / "tinystories/__init__.py",
                                                 submodule_search_locations=[str(source_dir / "tinystories")])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    from importlib import import_module
    return import_module(package_name + ".hardware_reference").FixedGPTNeo, pinned


def verify_reference(package_dir: Path, snapshot_dir: Path, fixture_path: Path,
                     output_path: Path, reference_source: Path | None = None) -> dict:
    if not snapshot_dir.is_dir() or not all((snapshot_dir / n).is_file() for n in ("config.json", "pytorch_model.bin")):
        raise ValueError("pinned snapshot is missing config or checkpoint")
    import base64
    snapshot_hashes = {"config.json": "/3TDDV67WrHaDy6kea33GXxQS0K1UiqFjDNKuR7UlYw=",
                       "pytorch_model.bin": "B/lgnqiCuBY/87I9QOK4LLcV1AljG+sVyEsWTzh32uc="}
    for name, expected in snapshot_hashes.items():
        if digest(snapshot_dir / name) != base64.b64decode(expected).hex():
            raise ValueError(f"pinned snapshot identity mismatch: {name}")
    package_hashes = authenticate_package(package_dir)
    if digest(fixture_path) != digest(REF / "tinystories-1m-fixed-point-continuation-oracle.json"):
        raise ValueError("continuation fixture differs from pinned oracle")
    if reference_source is None:
        reference_source = package_dir.parents[1]
    reference_class, sources = _pinned_numpy_reference(reference_source)
    sys.path.insert(0, str(ROOT))
    import torch
    from transformers import GPTNeoForCausalLM
    from TinyStories.model_adapter_exact_package import load_successor_exact_model
    bundle = load_successor_exact_model(REF / "tinystories-1m-exact-input-contract.json", package_dir,
                                        snapshot_dir, REF / "tinystories-1m-exact-generation.json")
    independent = reference_class(package_dir)
    fp32 = GPTNeoForCausalLM.from_pretrained(snapshot_dir, local_files_only=True).eval()
    fixture = json.loads(fixture_path.read_text())
    context = list(fixture["token_contract"]["initial_context"])
    comparisons = []
    with torch.no_grad():
        for index, step in enumerate(fixture["steps"]):
            ids = torch.tensor([context], dtype=torch.int64)
            actual = bundle.model(ids)[0, -1]
            expected = torch.from_numpy(independent.forward(context)[-1].copy())
            original = fp32(ids).logits[0, -1]
            token = int(torch.argmax(actual))
            if not torch.equal(actual, expected):
                raise ValueError(f"integer logits mismatch at step {index}")
            if token != step["selected_token"]["value"] or int(torch.argmax(original)) != token:
                raise ValueError(f"greedy token mismatch at step {index}")
            comparisons.append({"step": index, "context_length": len(context), "token": token,
                                "integer_logits_equal": True, "logit_count": actual.numel(),
                                "integer_logits_sha256": hashlib.sha256(actual.numpy().astype("<i8").tobytes()).hexdigest(),
                                "fp32_top1_equal": True,
                                "maximum_absolute_fp32_logit_difference": float(torch.max(torch.abs(actual.double() / 65536 - original.double())))})
            context.append(token)
    if len(comparisons) != 16 or any(c["logit_count"] != 50257 for c in comparisons):
        raise ValueError("closure fixture coverage differs from 16 full-vocabulary contexts")
    result = {"status": "passed", "scope": "frozen_prompt_16_contexts", "package_hashes": package_hashes,
              "snapshot_hashes": {n: digest(snapshot_dir / n) for n in snapshot_hashes},
              "reference_sources": sources, "fixture_sha256": digest(fixture_path), "comparisons": comparisons,
              "narrowing_bounds": validate_narrowing_bounds(package_dir),
              "limitations": ["No general-prompt or floating-point logit equivalence claim.",
                              "No RTL or hardware execution performed by this CPU gate."]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package", "snapshot", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--reference-source", type=Path, required=True)
    args = parser.parse_args()
    result = verify_reference(args.package, args.snapshot, args.fixture, args.output, args.reference_source)
    print(json.dumps({"status": result["status"], "contexts": len(result["comparisons"])}))


if __name__ == "__main__":
    main()
