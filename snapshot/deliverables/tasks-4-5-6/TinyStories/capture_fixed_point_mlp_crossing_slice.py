#!/usr/bin/env python3
"""Capture and replay the exact block-0 TinyStories-1M MLP crossing."""
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

from TinyStories.model_adapter_exact_package import (  # noqa: E402
    GEMV_NAMES,
    NONLINEAR_BOUNDARY_NAMES,
    QDQ_BOUNDARY_NAMES,
    Q_SCALE,
    Q_VALUE,
    activation_qdq,
    load_successor_exact_model,
    round_shift_signed,
    serial_gemv,
)

CONTRACT = ROOT / "artifacts/reference/tinystories-1m-exact-input-contract.json"
PACKAGE = Path(
    "/home/roland/kev-gpt/.worktrees/kintex-selftest/model_packages/tinystories-1m"
)
MODEL = Path(
    "/home/roland/.cache/huggingface/hub/models--roneneldan--TinyStories-1M/"
    "snapshots/77f1b168e219585646439073245fe87e56b3023e"
)
GENERATION_ARTIFACT = (
    ROOT / "artifacts/reference/tinystories-1m-exact-generation.json"
)
SCHEMA = "tinystories-1m-fixed-point-mlp-crossing-slice-v1"
PROMPT_TOKENS = [7454, 2402, 257, 640]

TENSOR_CONTRACT: dict[str, tuple[str, list[int]]] = {
    "c_fc_input_codes_i8": ("c_fc_input_signed_int8_codes", [4, 64]),
    "c_fc_input_scale_q8_24": ("c_fc_input_per_channel_q8_24_scale", [64]),
    "c_fc_input_q16_16": ("c_fc_input_dequantized_q16_16", [4, 64]),
    "c_fc_accumulator_i64": ("c_fc_exact_serial_accumulator_i64", [4, 256]),
    "c_fc_post_weight_rescale_bias_q16_16": (
        "c_fc_post_weight_rescale_plus_bias_q16_16",
        [4, 256],
    ),
    "c_fc_output_codes_i8": ("c_fc_output_signed_int8_codes", [4, 256]),
    "c_fc_output_scale_q8_24": (
        "c_fc_output_per_channel_q8_24_scale",
        [256],
    ),
    "c_fc_output_q16_16": ("c_fc_output_dequantized_q16_16", [4, 256]),
    "gelu_input_q16_16": ("block_0_fixed_gelu_input_q16_16", [4, 256]),
    "gelu_output_q16_16": ("block_0_fixed_gelu_output_q16_16", [4, 256]),
    "gelu_lut_q12": ("exact_fixed_gelu_lookup_table_q12", [8192]),
    "c_proj_input_codes_i8": ("c_proj_input_signed_int8_codes", [4, 256]),
    "c_proj_input_scale_q8_24": (
        "c_proj_input_per_channel_q8_24_scale",
        [256],
    ),
    "c_proj_input_q16_16": ("c_proj_input_dequantized_q16_16", [4, 256]),
    "c_proj_accumulator_i64": ("c_proj_exact_serial_accumulator_i64", [4, 64]),
    "c_proj_post_weight_rescale_bias_q16_16": (
        "c_proj_post_weight_rescale_plus_bias_q16_16",
        [4, 64],
    ),
    "c_proj_output_codes_i8": ("c_proj_output_signed_int8_codes", [4, 64]),
    "c_proj_output_scale_q8_24": (
        "c_proj_output_per_channel_q8_24_scale",
        [64],
    ),
    "c_proj_output_q16_16": ("c_proj_output_dequantized_q16_16", [4, 64]),
    "c_fc_weight_codes_i8": ("c_fc_weight_signed_int8_codes", [256, 64]),
    "c_fc_weight_scale_q8_24": (
        "c_fc_weight_per_output_q8_24_scale",
        [256],
    ),
    "c_fc_bias_q16_16": ("c_fc_bias_q16_16", [256]),
    "c_proj_weight_codes_i8": ("c_proj_weight_signed_int8_codes", [64, 256]),
    "c_proj_weight_scale_q8_24": (
        "c_proj_weight_per_output_q8_24_scale",
        [64],
    ),
    "c_proj_bias_q16_16": ("c_proj_bias_q16_16", [64]),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in ("semantic", "shape", "dtype", "values")
    }


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


def _fixture_binding_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    tensor_receipts = {
        name: {
            key: record[key]
            for key in (
                "semantic",
                "shape",
                "dtype",
                "canonical_sha256",
                "little_endian_int64_sha256",
                "bytes",
            )
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
        )
    } | {"tensor_receipts": tensor_receipts}


def _verify_observation_indices() -> None:
    expected_qdq = (
        "transformer.h.0.mlp.c_fc.input",
        "transformer.h.0.mlp.c_fc.output",
        "transformer.h.0.mlp.c_proj.input",
        "transformer.h.0.mlp.c_proj.output",
    )
    if QDQ_BOUNDARY_NAMES[8:12] != expected_qdq:
        raise ValueError("exact adapter MLP Q/DQ observation indices changed")
    if GEMV_NAMES[4:6] != (
        "transformer.h.0.mlp.c_fc",
        "transformer.h.0.mlp.c_proj",
    ):
        raise ValueError("exact adapter MLP accumulator indices changed")
    if NONLINEAR_BOUNDARY_NAMES[3] != "transformer.h.0.mlp.gelu.output":
        raise ValueError("exact adapter GELU observation index changed")


def _post_weight_rescale_bias(
    accumulator: torch.Tensor, weight_scale: torch.Tensor, bias: torch.Tensor
) -> torch.Tensor:
    return round_shift_signed(
        accumulator * weight_scale, 2 * Q_SCALE - Q_VALUE
    ) + bias


def _fixed_gelu(values_q16: torch.Tensor, lut_q12: torch.Tensor) -> torch.Tensor:
    q12 = torch.clamp(round_shift_signed(values_q16, 4), -32768, 32767)
    biased = q12 + 32768
    index = torch.bitwise_right_shift(biased, 3)
    fraction = torch.bitwise_and(biased, 7)
    upper = torch.minimum(index + 1, torch.full_like(index, 8191))
    interpolated = lut_q12[index] + torch.bitwise_right_shift(
        (lut_q12[upper] - lut_q12[index]) * fraction, 3
    )
    return torch.bitwise_left_shift(interpolated, 4)


def capture() -> dict[str, Any]:
    _verify_observation_indices()
    bundle = load_successor_exact_model(CONTRACT, PACKAGE, MODEL, GENERATION_ARTIFACT)
    prompt = torch.tensor(
        [bundle.contract["reference"]["prompt_tokens"]], dtype=torch.int64
    )
    if prompt[0].tolist() != PROMPT_TOKENS:
        raise ValueError("exact adapter frozen prompt changed")
    with torch.no_grad():
        _, _, qdq, accumulators, nonlinear = bundle.model._execute(prompt)

    if len(qdq) <= 35 or len(accumulators) <= 5 or len(nonlinear) <= 3:
        raise ValueError("exact adapter observation trace is incomplete")

    (
        c_fc_input_codes,
        c_fc_input_scale,
        c_fc_input_q16,
        c_fc_output_codes,
        c_fc_output_scale,
        c_fc_output_q16,
    ) = qdq[24:30]
    (
        c_proj_input_codes,
        c_proj_input_scale,
        c_proj_input_q16,
        c_proj_output_codes,
        c_proj_output_scale,
        c_proj_output_q16,
    ) = qdq[30:36]
    c_fc_accumulator = accumulators[4]
    c_proj_accumulator = accumulators[5]
    gelu_output_q16 = nonlinear[3].reshape(-1, 256)

    c_fc_weight_codes = bundle.model._buffer("code", "blocks.0.mlp.fc.weight")
    c_fc_weight_scale = bundle.model._buffer("scale", "blocks.0.mlp.fc.weight")
    c_fc_bias = bundle.model._buffer("parameter", "blocks.0.mlp.fc.bias")
    c_proj_weight_codes = bundle.model._buffer("code", "blocks.0.mlp.proj.weight")
    c_proj_weight_scale = bundle.model._buffer("scale", "blocks.0.mlp.proj.weight")
    c_proj_bias = bundle.model._buffer("parameter", "blocks.0.mlp.proj.bias")
    gelu_lut = bundle.model._gelu_lut

    c_fc_post = _post_weight_rescale_bias(
        c_fc_accumulator, c_fc_weight_scale, c_fc_bias
    )
    c_proj_post = _post_weight_rescale_bias(
        c_proj_accumulator, c_proj_weight_scale, c_proj_bias
    )
    if not torch.equal(_fixed_gelu(c_fc_output_q16, gelu_lut), gelu_output_q16):
        raise ValueError("captured exact GELU observation does not replay")

    tensors = {
        "c_fc_input_codes_i8": c_fc_input_codes,
        "c_fc_input_scale_q8_24": c_fc_input_scale,
        "c_fc_input_q16_16": c_fc_input_q16,
        "c_fc_accumulator_i64": c_fc_accumulator,
        "c_fc_post_weight_rescale_bias_q16_16": c_fc_post,
        "c_fc_output_codes_i8": c_fc_output_codes,
        "c_fc_output_scale_q8_24": c_fc_output_scale,
        "c_fc_output_q16_16": c_fc_output_q16,
        "gelu_input_q16_16": c_fc_output_q16,
        "gelu_output_q16_16": gelu_output_q16,
        "gelu_lut_q12": gelu_lut,
        "c_proj_input_codes_i8": c_proj_input_codes,
        "c_proj_input_scale_q8_24": c_proj_input_scale,
        "c_proj_input_q16_16": c_proj_input_q16,
        "c_proj_accumulator_i64": c_proj_accumulator,
        "c_proj_post_weight_rescale_bias_q16_16": c_proj_post,
        "c_proj_output_codes_i8": c_proj_output_codes,
        "c_proj_output_scale_q8_24": c_proj_output_scale,
        "c_proj_output_q16_16": c_proj_output_q16,
        "c_fc_weight_codes_i8": c_fc_weight_codes,
        "c_fc_weight_scale_q8_24": c_fc_weight_scale,
        "c_fc_bias_q16_16": c_fc_bias,
        "c_proj_weight_codes_i8": c_proj_weight_codes,
        "c_proj_weight_scale_q8_24": c_proj_weight_scale,
        "c_proj_bias_q16_16": c_proj_bias,
    }
    records = {
        name: tensor_record(tensor, TENSOR_CONTRACT[name][0])
        for name, tensor in tensors.items()
    }
    value: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "authenticated_frozen_prompt_mlp_fixture",
        "identity": {
            "contract_sha256": sha(CONTRACT),
            "package_manifest_sha256": sha(PACKAGE / "manifest.json"),
            "package_weights_sha256": sha(PACKAGE / "weights.bin"),
            "package_scales_sha256": sha(PACKAGE / "scales.bin"),
            "package_receipt_sha256": sha(PACKAGE / "receipt.json"),
            "model_config_sha256": sha(MODEL / "config.json"),
            "adapter_sha256": sha(ROOT / "TinyStories/model_adapter_exact_package.py"),
            "capture_sha256": sha(Path(__file__)),
            "successor_generation_receipt_sha256": sha(GENERATION_ARTIFACT),
        },
        "prompt_tokens": prompt[0].tolist(),
        "slice": {
            "layer": 0,
            "c_fc": {"rows": 4, "inputs": 64, "outputs": 256},
            "c_proj": {"rows": 4, "inputs": 256, "outputs": 64},
            "observation_indices": {
                "c_fc_qdq": [24, 30],
                "c_fc_accumulator": 4,
                "gelu_nonlinear": 3,
                "c_proj_qdq": [30, 36],
                "c_proj_accumulator": 5,
            },
        },
        "arithmetic": {
            "value_format": "signed_q16.16_int64_tensor",
            "scale_format": "unsigned_q8.24_int64_tensor",
            "activation_codes": "signed_int8_saturated",
            "gemv": "ascending_input_index_signed_int64_twos_complement_wrap",
            "post_weight_rescale": "signed_magnitude_half_up_shift_32_plus_q16.16_bias",
            "activation_qdq": "signed_int8_saturated_nearest_ties_away_from_zero",
            "gelu": "exact_q12_8192_entry_lut_linear_interpolation",
        },
        "tensors": records,
    }
    binding = canonical(_fixture_binding_payload(value))
    value["tensor_fixture_receipt_sha256"] = binding
    for record in records.values():
        record["fixture_receipt_sha256"] = binding
    value["receipt_sha256"] = canonical(value)
    return value


def _tensor_from_record(record: Mapping[str, Any]) -> torch.Tensor:
    try:
        return torch.tensor(record["values"], dtype=torch.int64).contiguous()
    except (TypeError, ValueError, RuntimeError) as error:
        raise ValueError("fixture tensor values are not rectangular int64 data") from error


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
        "tensors",
        "tensor_fixture_receipt_sha256",
        "receipt_sha256",
    }
    if set(value) != required_top_level:
        raise ValueError("fixture top-level field set mismatch")
    if value["schema"] != SCHEMA:
        raise ValueError("fixture schema mismatch")
    if value["status"] != "authenticated_frozen_prompt_mlp_fixture":
        raise ValueError("fixture authentication status mismatch")
    if value["prompt_tokens"] != PROMPT_TOKENS:
        raise ValueError("fixture frozen prompt mismatch")
    expected_slice = {
        "layer": 0,
        "c_fc": {"rows": 4, "inputs": 64, "outputs": 256},
        "c_proj": {"rows": 4, "inputs": 256, "outputs": 64},
        "observation_indices": {
            "c_fc_qdq": [24, 30],
            "c_fc_accumulator": 4,
            "gelu_nonlinear": 3,
            "c_proj_qdq": [30, 36],
            "c_proj_accumulator": 5,
        },
    }
    if value["slice"] != expected_slice:
        raise ValueError("fixture MLP slice contract mismatch")
    if not isinstance(value["identity"], dict) or not value["identity"]:
        raise ValueError("fixture identity is missing")
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in value["identity"].values()
    ):
        raise ValueError("fixture identity hash is malformed")

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
        if (
            record["semantic"] != semantic
            or record["shape"] != shape
            or record["dtype"] != "int64"
        ):
            raise ValueError(f"fixture tensor contract mismatch: {name}")
        tensor = _tensor_from_record(record)
        if list(tensor.shape) != shape:
            raise ValueError(f"fixture tensor value shape mismatch: {name}")
        if record["canonical_sha256"] != canonical(_record_payload(record)):
            raise ValueError(f"fixture tensor canonical hash mismatch: {name}")
        raw = tensor.numpy().astype("<i8", copy=False).tobytes()
        if (
            record["bytes"] != len(raw)
            or record["little_endian_int64_sha256"]
            != hashlib.sha256(raw).hexdigest()
        ):
            raise ValueError(f"fixture tensor raw bytes/hash mismatch: {name}")
        if record["fixture_receipt_sha256"] != binding:
            raise ValueError(f"fixture tensor receipt binding mismatch: {name}")

    if binding != canonical(_fixture_binding_payload(value)):
        raise ValueError("fixture tensor receipt mismatch")
    payload = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if value["receipt_sha256"] != canonical(payload):
        raise ValueError("fixture self-hash mismatch")
    return value


def verify_eager_replay(path: Path) -> None:
    expected = verify_fixture(path)
    actual = capture()
    if actual != expected:
        raise ValueError("fixture eager replay mismatch")


def _tensor(value: Mapping[str, Any], name: str) -> torch.Tensor:
    return _tensor_from_record(value["tensors"][name])


def _verify_qdq(
    value: Mapping[str, Any],
    prefix: str,
    source_q16: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    scales = _tensor(value, f"{prefix}_input_scale_q8_24")
    codes, dequantized = activation_qdq(source_q16, scales)
    if not torch.equal(codes, _tensor(value, f"{prefix}_input_codes_i8")):
        raise ValueError(f"fixture {prefix} input code Q/DQ replay mismatch")
    if not torch.equal(dequantized, _tensor(value, f"{prefix}_input_q16_16")):
        raise ValueError(f"fixture {prefix} input value Q/DQ replay mismatch")
    return codes, scales


def _verify_gemv(
    value: Mapping[str, Any],
    prefix: str,
    input_codes: torch.Tensor,
    input_scale: torch.Tensor,
) -> torch.Tensor:
    accumulator = serial_gemv(
        input_codes * input_scale, _tensor(value, f"{prefix}_weight_codes_i8")
    )
    if not torch.equal(accumulator, _tensor(value, f"{prefix}_accumulator_i64")):
        raise ValueError(f"fixture {prefix} exact GEMV replay mismatch")
    post = _post_weight_rescale_bias(
        accumulator,
        _tensor(value, f"{prefix}_weight_scale_q8_24"),
        _tensor(value, f"{prefix}_bias_q16_16"),
    )
    if not torch.equal(
        post, _tensor(value, f"{prefix}_post_weight_rescale_bias_q16_16")
    ):
        raise ValueError(f"fixture {prefix} post-weight replay mismatch")
    output_codes, output_q16 = activation_qdq(
        post, _tensor(value, f"{prefix}_output_scale_q8_24")
    )
    if not torch.equal(output_codes, _tensor(value, f"{prefix}_output_codes_i8")):
        raise ValueError(f"fixture {prefix} output code Q/DQ replay mismatch")
    if not torch.equal(output_q16, _tensor(value, f"{prefix}_output_q16_16")):
        raise ValueError(f"fixture {prefix} output value Q/DQ replay mismatch")
    return output_q16


def verify_fixed_point_replay(path: Path) -> None:
    value = verify_fixture(path)
    c_fc_source = _tensor(value, "c_fc_input_q16_16")
    c_fc_codes, c_fc_scale = _verify_qdq(value, "c_fc", c_fc_source)
    c_fc_output = _verify_gemv(value, "c_fc", c_fc_codes, c_fc_scale)
    if not torch.equal(c_fc_output, _tensor(value, "gelu_input_q16_16")):
        raise ValueError("fixture c_fc-to-GELU boundary mismatch")

    gelu_output = _fixed_gelu(c_fc_output, _tensor(value, "gelu_lut_q12"))
    if not torch.equal(gelu_output, _tensor(value, "gelu_output_q16_16")):
        raise ValueError("fixture exact GELU replay mismatch")

    c_proj_codes, c_proj_scale = _verify_qdq(value, "c_proj", gelu_output)
    _verify_gemv(value, "c_proj", c_proj_codes, c_proj_scale)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify_eager_replay(args.output)
        verify_fixed_point_replay(args.output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(capture(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
