"""Build TinyStories family packages with the accepted 1M W8/A8 rules.

The pinned kev-gpt package utilities are used for *model data only*. No
accelerator RTL or top-level implementation is imported by this script.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from TinyStories.model_adapter_fixed_point_family import import_family_model
from scripts.pipeline.tinystories_family_spec import _expected_qdq


def validate_activation_scale_names(scales: dict[str, object], layers: int) -> None:
    expected = _expected_qdq(layers)
    actual = set(scales)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"activation scale names: missing={missing[:3]} extra={extra[:3]}")


def build_family_package(
    snapshot: Path,
    output: Path,
    *,
    revision: str,
    calibration_ids: Path,
    activation_scales: dict[str, object] | None = None,
    max_context: int = 32,
) -> dict[str, object]:
    """Serialize the same per-output INT8 package as the accepted 1M path."""
    from tinystories.package_io import write_package
    from tinystories.quantize import quantize_model

    imported = import_family_model(snapshot, revision=revision, max_context=max_context)
    ids = np.fromfile(calibration_ids, dtype="<i4")
    if ids.size == 0 or calibration_ids.stat().st_size != ids.size * 4:
        raise ValueError("calibration IDs must be nonempty signed little-endian int32")
    if activation_scales is None:
        from transformers import GPTNeoForCausalLM
        from tinystories.build_package import _calibrate_activation_scales

        source = GPTNeoForCausalLM.from_pretrained(snapshot, local_files_only=True)
        activation_scales = _calibrate_activation_scales(source, ids)
    validate_activation_scale_names(activation_scales, imported.manifest["n_layer"])
    overrides = {
        name: 8 for name, value in imported.tensors.items()
        if name != "lm_head.weight" and name.endswith(".weight") and value.ndim == 2
    }
    package = quantize_model(
        imported, ids, max_context=max_context,
        bits_by_tensor=overrides, activation_scales=activation_scales,
    )
    output.mkdir(parents=True, exist_ok=True)
    manifest = write_package(package, output)
    for name in imported.tokenizer_files:
        shutil.copyfile(Path(snapshot) / name, output / name)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--calibration-ids", type=Path, required=True)
    parser.add_argument("--activation-scales-json", type=Path)
    parser.add_argument("--max-context", type=int, default=32)
    args = parser.parse_args()
    scales = None
    if args.activation_scales_json:
        value = json.loads(args.activation_scales_json.read_text(encoding="utf-8"))
        scales = value.get("activation_scales", value)
    manifest = build_family_package(
        args.snapshot, args.output, revision=args.revision,
        calibration_ids=args.calibration_ids, activation_scales=scales,
        max_context=args.max_context,
    )
    print(json.dumps({
        "revision": args.revision,
        "tensor_count": len(manifest["tensors"]),
        "activation_boundaries": len(manifest["activation_scales"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
