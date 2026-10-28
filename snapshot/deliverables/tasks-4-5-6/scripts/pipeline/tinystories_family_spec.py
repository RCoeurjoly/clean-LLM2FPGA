"""Validated fixed-point GPT-Neo family geometry and tokenizer contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Sequence


FROZEN_PROMPT = "Once upon a time"
FROZEN_PROMPT_IDS = (7454, 2402, 257, 640)
TOKENIZER_FILES = ("merges.txt", "special_tokens_map.json", "tokenizer.json",
                   "tokenizer_config.json", "vocab.json")


class FamilySpecError(ValueError):
    """A checkpoint, package or tokenizer violates the family contract."""


def _positive(mapping: dict, name: str) -> int:
    value = mapping.get(name)
    if type(value) is not int or value < 1:
        raise FamilySpecError(f"{name}: expected positive integer")
    return value


def _expected_shapes(layers: int, hidden: int, vocab: int, context: int) -> dict[str, tuple[int, ...]]:
    shapes = {
        "token_embedding.weight": (vocab, hidden),
        "position_embedding.weight": (context, hidden),
        "final_ln.weight": (hidden,), "final_ln.bias": (hidden,),
    }
    for layer in range(layers):
        prefix = f"blocks.{layer}"
        shapes.update({
            f"{prefix}.ln1.weight": (hidden,), f"{prefix}.ln1.bias": (hidden,),
            f"{prefix}.attn.q.weight": (hidden, hidden),
            f"{prefix}.attn.k.weight": (hidden, hidden),
            f"{prefix}.attn.v.weight": (hidden, hidden),
            f"{prefix}.attn.out.weight": (hidden, hidden),
            f"{prefix}.attn.out.bias": (hidden,),
            f"{prefix}.ln2.weight": (hidden,), f"{prefix}.ln2.bias": (hidden,),
            f"{prefix}.mlp.fc.weight": (4 * hidden, hidden),
            f"{prefix}.mlp.fc.bias": (4 * hidden,),
            f"{prefix}.mlp.proj.weight": (hidden, 4 * hidden),
            f"{prefix}.mlp.proj.bias": (hidden,),
        })
    return shapes


def _expected_qdq(layers: int) -> set[str]:
    names = {"lm_head.input"}
    for layer in range(layers):
        for op in ("attn.attention.q_proj", "attn.attention.k_proj",
                   "attn.attention.v_proj", "attn.attention.out_proj",
                   "mlp.c_fc", "mlp.c_proj"):
            for phase in ("input", "output"):
                names.add(f"transformer.h.{layer}.{op}.{phase}")
    return names


def _attention_pattern(config: dict, layers: int) -> tuple[str, ...]:
    entries = config.get("attention_types")
    if not isinstance(entries, list):
        raise FamilySpecError("attention_pattern: missing attention_types")
    pattern: list[str] = []
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2
                and isinstance(entry[0], list) and type(entry[1]) is int and entry[1] > 0):
            raise FamilySpecError("attention_pattern: malformed attention_types")
        pattern.extend(entry[0] * entry[1])
    if layers % 2 or tuple(pattern) != ("global", "local") * (layers // 2):
        raise FamilySpecError("attention_pattern: expected alternating global/local GPT-Neo layers")
    return tuple(pattern)


def _tokenizer_identity(snapshot: Path, package: Path) -> str:
    digest = hashlib.sha256()
    for name in TOKENIZER_FILES:
        source = snapshot / name
        copied = package / name
        if not source.is_file() or not copied.is_file():
            raise FamilySpecError(f"tokenizer: missing {name} in snapshot or package")
        data = source.read_bytes()
        if copied.read_bytes() != data:
            raise FamilySpecError(f"tokenizer: package {name} differs from pinned snapshot")
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "little") + data)
    return digest.hexdigest()


@dataclass(frozen=True)
class FamilyModelSpec:
    revision: str
    layers: int
    hidden: int
    heads: int
    head_dim: int
    mlp_width: int
    vocab: int
    context: int
    attention_pattern: tuple[str, ...]
    qdq_names: tuple[str, ...]
    tokenizer_identity: str
    source_shapes: Mapping[str, tuple[int, ...]]
    frozen_prompt_ids: tuple[int, ...]

    @classmethod
    def from_metadata(cls, config: dict, manifest: dict, *, revision: str,
                      tokenizer_identity: str,
                      frozen_prompt_ids: Sequence[int]) -> FamilyModelSpec:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise FamilySpecError("revision: expected immutable 40-digit commit")
        if config.get("model_type") != "gpt_neo":
            raise FamilySpecError("model_type: expected gpt_neo")
        layers = _positive(config, "num_layers")
        hidden = _positive(config, "hidden_size")
        heads = _positive(config, "num_heads")
        vocab = _positive(config, "vocab_size")
        positions = _positive(config, "max_position_embeddings")
        window = _positive(config, "window_size")
        if hidden % heads:
            raise FamilySpecError("num_heads: does not divide hidden_size")
        if config.get("activation_function") != "gelu_new":
            raise FamilySpecError("activation_function: expected gelu_new")
        if config.get("tie_word_embeddings", True) is not True:
            raise FamilySpecError("tie_word_embeddings: untied LM head unsupported")
        pattern = _attention_pattern(config, layers)
        model = manifest.get("model")
        if not isinstance(model, dict):
            raise FamilySpecError("package model metadata missing")
        context = _positive(model, "max_context")
        if context > min(positions, window):
            raise FamilySpecError("max_context: exceeds position or local attention window")
        expected_model = {"source_revision": revision, "n_layer": layers,
                          "hidden_size": hidden, "n_head": heads,
                          "head_dim": hidden // heads, "vocab_size": vocab,
                          "tie_word_embeddings": True}
        for key, expected in expected_model.items():
            if model.get(key) != expected:
                raise FamilySpecError(f"package model {key}: expected {expected}")
        expected_shapes = _expected_shapes(layers, hidden, vocab, context)
        tensors = manifest.get("tensors")
        if not isinstance(tensors, dict) or set(tensors) != set(expected_shapes):
            missing = sorted(set(expected_shapes) - set(tensors or {}))
            extra = sorted(set(tensors or {}) - set(expected_shapes))
            raise FamilySpecError(f"source_shapes: missing={missing[:3]} extra={extra[:3]}")
        for name, shape in expected_shapes.items():
            actual = tensors[name].get("logical_shape")
            if actual != list(shape):
                raise FamilySpecError(f"source_shapes: {name}: expected {shape}, got {actual}")
        scales = manifest.get("activation_scales")
        expected_qdq = _expected_qdq(layers)
        if not isinstance(scales, dict) or set(scales) != expected_qdq:
            missing = sorted(expected_qdq - set(scales or {}))
            extra = sorted(set(scales or {}) - expected_qdq)
            raise FamilySpecError(f"qdq_names: missing={missing[:3]} extra={extra[:3]}")
        if not tokenizer_identity or not frozen_prompt_ids:
            raise FamilySpecError("tokenizer: identity or frozen prompt IDs absent")
        return cls(revision, layers, hidden, heads, hidden // heads, 4 * hidden,
                   vocab, context, pattern, tuple(sorted(scales)), tokenizer_identity,
                   MappingProxyType(expected_shapes), tuple(frozen_prompt_ids))

    @classmethod
    def from_package(cls, snapshot: Path, package: Path, revision: str) -> FamilyModelSpec:
        from transformers import AutoTokenizer

        config = json.loads((snapshot / "config.json").read_text())
        manifest = json.loads((package / "manifest.json").read_text())
        identity = _tokenizer_identity(snapshot, package)
        tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
        ids = tuple(tokenizer.encode(FROZEN_PROMPT, add_special_tokens=False))
        if len(tokenizer) != 50257 or ids != FROZEN_PROMPT_IDS:
            raise FamilySpecError(f"tokenizer: frozen prompt IDs or vocabulary differ: {ids}")
        return cls.from_metadata(config, manifest, revision=revision,
                                 tokenizer_identity=identity, frozen_prompt_ids=ids)

    def validate_prompt(self, ids: Sequence[int]) -> None:
        if tuple(ids) != self.frozen_prompt_ids:
            raise FamilySpecError("tokenizer: supplied IDs do not encode the frozen prompt")
