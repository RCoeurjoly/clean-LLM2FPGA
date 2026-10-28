"""Parameterize the accepted GPT-Neo tensor mapping for TinyStories scaling.

This is model-data preparation, not accelerator RTL. The 1M package remains
immutable; equivalence to its canonical tensor order is an external gate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from scripts.pipeline.tinystories_family_spec import FamilySpecError, _attention_pattern


class FamilyModelError(ValueError):
    """A checkpoint cannot satisfy the fixed-point family tensor contract."""


@dataclass(frozen=True)
class ImportedFamilyGPTNeo:
    tensors: dict[str, np.ndarray]
    manifest: dict[str, object]
    source_config: dict[str, object]
    tokenizer_files: tuple[str, ...]


def _integer(config: dict[str, object], key: str) -> int:
    value = config.get(key)
    if type(value) is not int or value <= 0:
        raise FamilyModelError(f"{key} must be a positive integer")
    return value


def _array(state: dict[str, torch.Tensor], name: str, shape: tuple[int, ...]) -> np.ndarray:
    value = state.get(name)
    if not isinstance(value, torch.Tensor):
        raise FamilyModelError(f"missing checkpoint tensor {name}")
    array = np.ascontiguousarray(value.detach().to(torch.float32).cpu().numpy())
    if array.shape != shape:
        raise FamilyModelError(f"{name}: expected {shape}, got {array.shape}")
    return array


def _matrix(state: dict[str, torch.Tensor], name: str, shape: tuple[int, int]) -> np.ndarray:
    value = state.get(name)
    if not isinstance(value, torch.Tensor):
        raise FamilyModelError(f"missing checkpoint tensor {name}")
    array = np.ascontiguousarray(value.detach().to(torch.float32).cpu().numpy())
    if array.shape == shape:
        return array
    if array.shape == (shape[1], shape[0]):
        return np.ascontiguousarray(array.T)
    raise FamilyModelError(f"{name}: expected {shape} or its transpose, got {array.shape}")


def import_family_model(
    snapshot: Path, *, revision: str, max_context: int = 32
) -> ImportedFamilyGPTNeo:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise FamilyModelError("revision must be an immutable commit")
    snapshot = Path(snapshot)
    config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("model_type") != "gpt_neo":
        raise FamilyModelError("model_type must be gpt_neo")
    layers = _integer(config, "num_layers")
    hidden = _integer(config, "hidden_size")
    heads = _integer(config, "num_heads")
    vocab = _integer(config, "vocab_size")
    positions = _integer(config, "max_position_embeddings")
    window = _integer(config, "window_size")
    if hidden % heads:
        raise FamilyModelError("num_heads must divide hidden_size")
    if not 0 < max_context <= min(positions, window):
        raise FamilyModelError("max_context exceeds position or local attention capacity")
    if config.get("activation_function") != "gelu_new":
        raise FamilyModelError("activation_function must be gelu_new")
    if config.get("tie_word_embeddings", True) is not True:
        raise FamilyModelError("tie_word_embeddings must be true")
    try:
        _attention_pattern(config, layers)
    except FamilySpecError as error:
        raise FamilyModelError(str(error)) from error

    state = torch.load(snapshot / "pytorch_model.bin", map_location="cpu", weights_only=True, mmap=True)
    if not isinstance(state, dict):
        raise FamilyModelError("checkpoint is not a state dictionary")
    embedding = _array(state, "transformer.wte.weight", (vocab, hidden))
    head_weight = state.get("lm_head.weight")
    if head_weight is not None and (not isinstance(head_weight, torch.Tensor)
                                   or tuple(head_weight.shape) != (vocab, hidden)
                                   or not torch.equal(head_weight, state["transformer.wte.weight"])):
        raise FamilyModelError("lm_head.weight must be tied to token embedding")
    full_positions = _array(state, "transformer.wpe.weight", (positions, hidden))
    tensors = {
        "token_embedding.weight": embedding,
        "lm_head.weight": embedding,
        "position_embedding.weight": np.ascontiguousarray(full_positions[:max_context]),
        "final_ln.weight": _array(state, "transformer.ln_f.weight", (hidden,)),
        "final_ln.bias": _array(state, "transformer.ln_f.bias", (hidden,)),
    }
    mlp = 4 * hidden
    for layer in range(layers):
        source = f"transformer.h.{layer}"
        target = f"blocks.{layer}"
        for src_suffix, dst_suffix, shape in (
            ("ln_1.weight", "ln1.weight", (hidden,)),
            ("ln_1.bias", "ln1.bias", (hidden,)),
            ("attn.attention.q_proj.weight", "attn.q.weight", (hidden, hidden)),
            ("attn.attention.k_proj.weight", "attn.k.weight", (hidden, hidden)),
            ("attn.attention.v_proj.weight", "attn.v.weight", (hidden, hidden)),
            ("attn.attention.out_proj.weight", "attn.out.weight", (hidden, hidden)),
            ("attn.attention.out_proj.bias", "attn.out.bias", (hidden,)),
            ("ln_2.weight", "ln2.weight", (hidden,)),
            ("ln_2.bias", "ln2.bias", (hidden,)),
            ("mlp.c_fc.bias", "mlp.fc.bias", (mlp,)),
            ("mlp.c_proj.bias", "mlp.proj.bias", (hidden,)),
        ):
            tensors[f"{target}.{dst_suffix}"] = _array(state, f"{source}.{src_suffix}", shape)
        tensors[f"{target}.mlp.fc.weight"] = _matrix(state, f"{source}.mlp.c_fc.weight", (mlp, hidden))
        tensors[f"{target}.mlp.proj.weight"] = _matrix(state, f"{source}.mlp.c_proj.weight", (hidden, mlp))
    manifest = {
        "schema_version": 1,
        "model_type": "gpt_neo",
        "n_layer": layers,
        "hidden_size": hidden,
        "n_head": heads,
        "vocab_size": vocab,
        "source_revision": revision,
        "max_context": max_context,
        "local_window": window,
        "tie_word_embeddings": True,
        "activation_function": "gelu_new",
        "head_dim": hidden // heads,
    }
    tokenizer_files = tuple(sorted(
        path.name for path in snapshot.iterdir()
        if path.name in {"merges.txt", "special_tokens_map.json", "tokenizer.json",
                         "tokenizer_config.json", "vocab.json"}
    ))
    return ImportedFamilyGPTNeo(tensors, manifest, config, tokenizer_files)
