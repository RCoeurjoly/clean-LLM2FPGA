#!/usr/bin/env python3
"""Capture and replay the exact block-0 TinyStories-1M final residual."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from TinyStories import capture_fixed_point_attention_crossing_slice as capture_attention  # noqa: E402
from TinyStories import capture_fixed_point_mlp_crossing_slice as capture_mlp  # noqa: E402


ATTENTION_FIXTURE = (
    ROOT
    / "artifacts/reference/tinystories-1m-fixed-point-attention-crossing-slice.json"
)
MLP_FIXTURE = (
    ROOT / "artifacts/reference/tinystories-1m-fixed-point-mlp-crossing-slice.json"
)
SCHEMA = "tinystories-1m-fixed-point-block-composition-slice-v1"
STATUS = "authenticated_frozen_prompt_block_composition_fixture"
PROMPT_TOKENS = [7454, 2402, 257, 640]
SLICE_CONTRACT = {"block": 0, "rows": 4, "width": 64}
ARITHMETIC_CONTRACT = {
    "value_format": "signed_q16.16_int64_tensor",
    "residual_addition": "signed_int64_twos_complement_wrap",
}
TENSOR_CONTRACT = {
    "block_output_q16_16": ("block_0_output_q16_16", [4, 64]),
}
COMMON_IDENTITY_KEYS = (
    "contract_sha256",
    "package_manifest_sha256",
    "package_weights_sha256",
    "package_scales_sha256",
    "package_receipt_sha256",
    "model_config_sha256",
    "adapter_sha256",
    "successor_generation_receipt_sha256",
)
RECEIPT_FIELDS = (
    "semantic",
    "shape",
    "dtype",
    "canonical_sha256",
    "little_endian_int64_sha256",
    "bytes",
    "fixture_receipt_sha256",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("semantic", "shape", "dtype", "values")}


def _tensor_from_record(record: Mapping[str, Any]) -> torch.Tensor:
    try:
        return torch.tensor(record["values"], dtype=torch.int64).contiguous()
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError("fixture tensor values are not rectangular int64 data") from error


def tensor_record(value: torch.Tensor, semantic: str) -> dict[str, Any]:
    tensor = value.detach().cpu().contiguous().to(torch.int64)
    raw = tensor.numpy().astype("<i8", copy=False).tobytes()
    payload = {
        "semantic": semantic,
        "shape": list(tensor.shape),
        "dtype": "int64",
        "values": tensor.tolist(),
    }
    return {
        **payload,
        "canonical_sha256": canonical(payload),
        "little_endian_int64_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _validate_link_compatibility(
    attention: Mapping[str, Any], mlp: Mapping[str, Any]
) -> None:
    if attention["prompt_tokens"] != PROMPT_TOKENS or mlp["prompt_tokens"] != PROMPT_TOKENS:
        raise ValueError("linked fixture frozen prompt mismatch")
    for key in COMMON_IDENTITY_KEYS:
        if attention["identity"].get(key) != mlp["identity"].get(key):
            raise ValueError(f"linked fixture source identity mismatch: {key}")
    linked_mlp = attention.get("linked_mlp")
    if not isinstance(linked_mlp, dict):
        raise ValueError("attention fixture does not authenticate its MLP boundary")
    if linked_mlp.get("receipt_sha256") != mlp["receipt_sha256"]:
        raise ValueError("attention fixture linked MLP receipt mismatch")
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        linked = linked_mlp.get(name)
        expected = mlp["tensors"][name]
        if not isinstance(linked, dict) or any(
            linked.get(field) != expected[field]
            for field in RECEIPT_FIELDS
            if field != "fixture_receipt_sha256"
        ):
            raise ValueError(f"attention fixture linked MLP record mismatch: {name}")


def _linked_fixture_authority(
    fixture: Mapping[str, Any], fixture_path: Path
) -> dict[str, Any]:
    return {
        "path": fixture_path.relative_to(ROOT).as_posix(),
        "file_sha256": sha(fixture_path),
        "schema": fixture["schema"],
        "status": fixture["status"],
        "identity": fixture["identity"],
        "prompt_tokens": fixture["prompt_tokens"],
        "slice": fixture["slice"],
        "arithmetic": fixture["arithmetic"],
        "tensor_fixture_receipt_sha256": fixture["tensor_fixture_receipt_sha256"],
        "receipt_sha256": fixture["receipt_sha256"],
        "tensor_receipts": {
            name: {field: record[field] for field in RECEIPT_FIELDS}
            for name, record in fixture["tensors"].items()
        },
    }


def _expected_identity(
    attention: Mapping[str, Any], mlp: Mapping[str, Any]
) -> dict[str, str]:
    return {
        **{key: attention["identity"][key] for key in COMMON_IDENTITY_KEYS},
        "capture_sha256": sha(Path(__file__)),
        "attention_fixture_file_sha256": sha(ATTENTION_FIXTURE),
        "mlp_fixture_file_sha256": sha(MLP_FIXTURE),
    }


def _fixture_binding_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    tensor_receipts = {
        name: {
            field: record[field]
            for field in RECEIPT_FIELDS
            if field != "fixture_receipt_sha256"
        }
        for name, record in value["tensors"].items()
    }
    return {
        key: value[key]
        for key in (
            "schema",
            "status",
            "identity",
            "prompt_tokens",
            "slice",
            "arithmetic",
            "linked_attention",
            "linked_mlp",
        )
    } | {"tensor_receipts": tensor_receipts}


def _load_linked_fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    attention = capture_attention.verify_fixture(ATTENTION_FIXTURE)
    mlp = capture_mlp.verify_fixture(MLP_FIXTURE)
    _validate_link_compatibility(attention, mlp)
    return attention, mlp


def _block_output(
    attention: Mapping[str, Any], mlp: Mapping[str, Any]
) -> torch.Tensor:
    attention_residual = _tensor_from_record(
        attention["tensors"]["attention_residual_q16_16"]
    )
    c_proj_output = _tensor_from_record(mlp["tensors"]["c_proj_output_q16_16"])
    if list(attention_residual.shape) != [4, 64] or list(c_proj_output.shape) != [4, 64]:
        raise ValueError("linked final residual operand shape mismatch")
    return attention_residual + c_proj_output


def capture() -> dict[str, Any]:
    attention, mlp = _load_linked_fixtures()
    output = _block_output(attention, mlp)
    records = {
        "block_output_q16_16": tensor_record(
            output, TENSOR_CONTRACT["block_output_q16_16"][0]
        )
    }
    value: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "identity": _expected_identity(attention, mlp),
        "prompt_tokens": PROMPT_TOKENS,
        "slice": SLICE_CONTRACT,
        "arithmetic": ARITHMETIC_CONTRACT,
        "linked_attention": _linked_fixture_authority(attention, ATTENTION_FIXTURE),
        "linked_mlp": _linked_fixture_authority(mlp, MLP_FIXTURE),
        "tensors": records,
    }
    binding = canonical(_fixture_binding_payload(value))
    value["tensor_fixture_receipt_sha256"] = binding
    for record in records.values():
        record["fixture_receipt_sha256"] = binding
    value["receipt_sha256"] = canonical(value)
    return value


def verify_fixture(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"fixture is not readable canonical JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("fixture root must be an object")
    required_top_level = {
        "schema",
        "status",
        "identity",
        "prompt_tokens",
        "slice",
        "arithmetic",
        "linked_attention",
        "linked_mlp",
        "tensors",
        "tensor_fixture_receipt_sha256",
        "receipt_sha256",
    }
    if set(value) != required_top_level:
        raise ValueError("fixture top-level field set mismatch")

    attention, mlp = _load_linked_fixtures()
    expected_scalars = {
        "schema": SCHEMA,
        "status": STATUS,
        "identity": _expected_identity(attention, mlp),
        "prompt_tokens": PROMPT_TOKENS,
        "slice": SLICE_CONTRACT,
        "arithmetic": ARITHMETIC_CONTRACT,
        "linked_attention": _linked_fixture_authority(attention, ATTENTION_FIXTURE),
        "linked_mlp": _linked_fixture_authority(mlp, MLP_FIXTURE),
    }
    for name, expected in expected_scalars.items():
        if value[name] != expected:
            raise ValueError(f"fixture {name.replace('_', ' ')} mismatch")

    tensors = value["tensors"]
    if not isinstance(tensors, dict) or set(tensors) != set(TENSOR_CONTRACT):
        raise ValueError("fixture tensor set mismatch")
    binding = value["tensor_fixture_receipt_sha256"]
    for name, (semantic, shape) in TENSOR_CONTRACT.items():
        record = tensors[name]
        if not isinstance(record, dict) or set(record) != {
            "semantic",
            "shape",
            "dtype",
            "values",
            "canonical_sha256",
            "little_endian_int64_sha256",
            "bytes",
            "fixture_receipt_sha256",
        }:
            raise ValueError(f"fixture tensor record field set mismatch: {name}")
        if record["semantic"] != semantic or record["shape"] != shape or record["dtype"] != "int64":
            raise ValueError(f"fixture tensor contract mismatch: {name}")
        tensor = _tensor_from_record(record)
        if list(tensor.shape) != shape:
            raise ValueError(f"fixture tensor value shape mismatch: {name}")
        if record["canonical_sha256"] != canonical(_record_payload(record)):
            raise ValueError(f"fixture tensor canonical hash mismatch: {name}")
        raw = tensor.numpy().astype("<i8", copy=False).tobytes()
        if record["bytes"] != len(raw) or record["little_endian_int64_sha256"] != hashlib.sha256(raw).hexdigest():
            raise ValueError(f"fixture tensor raw bytes/hash mismatch: {name}")
        if record["fixture_receipt_sha256"] != binding:
            raise ValueError(f"fixture tensor receipt binding mismatch: {name}")

    if binding != canonical(_fixture_binding_payload(value)):
        raise ValueError("fixture tensor receipt mismatch")
    payload = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if value["receipt_sha256"] != canonical(payload):
        raise ValueError("fixture self-hash mismatch")
    return value


def verify_block_output_replay(path: Path) -> None:
    value = verify_fixture(path)
    attention, mlp = _load_linked_fixtures()
    actual = _block_output(attention, mlp)
    expected = _tensor_from_record(value["tensors"]["block_output_q16_16"])
    if not torch.equal(actual, expected):
        raise ValueError("fixture block output residual replay mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify_fixture(args.output)
        verify_block_output_replay(args.output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(capture(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
