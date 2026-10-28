#!/usr/bin/env python3
"""Inventory content-pinned local TinyStories GPT-Neo checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import torch
from transformers import GPTNeoConfig, GPTNeoForCausalLM


class InventoryError(ValueError):
    """A purported family checkpoint does not satisfy the inventory contract."""


FAMILY_LABELS = ("1M", "3M", "8M", "28M", "33M")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(config: dict[str, Any], name: str, alternative: str) -> int:
    value = config.get(name, config.get(alternative))
    if type(value) is not int or value <= 0:
        raise InventoryError(f"{name} must be a positive integer")
    return value


def inventory_model(
    *,
    size_label: str,
    model_id: str,
    revision: str,
    snapshot: Path,
    config_sha256: str,
    weights_sha256: str,
) -> dict[str, object]:
    if model_id != f"roneneldan/TinyStories-{size_label}":
        raise InventoryError("model_id does not match size_label")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise InventoryError("revision must be an immutable 40-digit commit")
    config_path = snapshot / "config.json"
    weights_path = snapshot / "pytorch_model.bin"
    for name, path, expected in (
        ("config_sha256", config_path, config_sha256),
        ("weights_sha256", weights_path, weights_sha256),
    ):
        if not path.is_file() or _sha256(path) != expected:
            raise InventoryError(f"{name} mismatch or missing file")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("model_type") != "gpt_neo":
        raise InventoryError("model_type must be gpt_neo")
    n_layer = _positive_int(config, "num_layers", "n_layer")
    n_head = _positive_int(config, "num_heads", "n_head")
    n_embd = _positive_int(config, "hidden_size", "n_embd")
    vocab_size = _positive_int(config, "vocab_size", "vocab_size")
    try:
        state = torch.load(weights_path, map_location="cpu", weights_only=True, mmap=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise InventoryError("weights could not be safely loaded") from error
    if not isinstance(state, dict) or not state:
        raise InventoryError("weights must contain a nonempty state dictionary")
    with torch.device("meta"):
        model = GPTNeoForCausalLM(GPTNeoConfig.from_dict(config))
    parameters = dict(model.named_parameters())
    for name, tensor in state.items():
        if not isinstance(name, str) or not isinstance(tensor, torch.Tensor):
            raise InventoryError("weights must contain only named tensors")
    for name, parameter in parameters.items():
        if name not in state or tuple(state[name].shape) != tuple(parameter.shape):
            raise InventoryError(f"checkpoint parameter missing or wrong shape: {name}")
    return {
        "size_label": size_label,
        "model_id": model_id,
        "revision": revision,
        "config_sha256": config_sha256,
        "weights_sha256": weights_sha256,
        "parameters": sum(parameter.numel() for parameter in parameters.values()),
        "n_layer": n_layer,
        "n_head": n_head,
        "n_embd": n_embd,
        "vocab_size": vocab_size,
    }


def inventory_family(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    labels = [entry.get("size_label") for entry in entries]
    if len(labels) != len(FAMILY_LABELS) or set(labels) != set(FAMILY_LABELS):
        raise InventoryError("family labels must be exactly 1M/3M/8M/28M/33M")
    by_label = {str(entry["size_label"]): entry for entry in entries}
    return [
        inventory_model(**{**by_label[label], "snapshot": Path(str(by_label[label]["snapshot"]))})
        for label in FAMILY_LABELS
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not all(isinstance(entry, dict) for entry in manifest):
        raise InventoryError("manifest must be a list of model records")
    rows = inventory_family(manifest)
    args.output.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
