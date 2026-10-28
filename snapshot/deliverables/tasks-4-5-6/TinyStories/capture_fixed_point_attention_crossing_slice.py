#!/usr/bin/env python3
"""Capture and replay the exact block-0 TinyStories-1M attention crossing."""
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

from TinyStories import capture_fixed_point_mlp_crossing_slice as capture_mlp  # noqa: E402
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
GENERATION_ARTIFACT = ROOT / "artifacts/reference/tinystories-1m-exact-generation.json"
MLP_FIXTURE = (
    ROOT / "artifacts/reference/tinystories-1m-fixed-point-mlp-crossing-slice.json"
)
SCHEMA = "tinystories-1m-fixed-point-attention-crossing-slice-v1"
PROMPT_TOKENS = [7454, 2402, 257, 640]


TENSOR_CONTRACT: dict[str, tuple[str, list[int]]] = {
    "block_input_q16_16": ("block_0_input_q16_16", [4, 64]),
    "ln1_gamma_q16_16": ("block_0_ln1_gamma_q16_16", [64]),
    "ln1_beta_q16_16": ("block_0_ln1_beta_q16_16", [64]),
    "ln1_output_q16_16": ("block_0_ln1_output_q16_16", [4, 64]),
}
for _prefix, _semantic in (
    ("q", "query"),
    ("k", "key"),
    ("v", "value"),
    ("out", "attention_output"),
):
    TENSOR_CONTRACT.update(
        {
            f"{_prefix}_input_codes_i8": (
                f"{_semantic}_input_signed_int8_codes",
                [4, 64],
            ),
            f"{_prefix}_input_scale_q8_24": (
                f"{_semantic}_input_per_channel_q8_24_scale",
                [64],
            ),
            f"{_prefix}_input_q16_16": (
                f"{_semantic}_input_dequantized_q16_16",
                [4, 64],
            ),
            f"{_prefix}_accumulator_i64": (
                f"{_semantic}_exact_serial_accumulator_i64",
                [4, 64],
            ),
            f"{_prefix}_post_weight_rescale_bias_q16_16": (
                f"{_semantic}_post_weight_rescale_plus_optional_bias_q16_16",
                [4, 64],
            ),
            f"{_prefix}_output_codes_i8": (
                f"{_semantic}_output_signed_int8_codes",
                [4, 64],
            ),
            f"{_prefix}_output_scale_q8_24": (
                f"{_semantic}_output_per_channel_q8_24_scale",
                [64],
            ),
            f"{_prefix}_output_q16_16": (
                f"{_semantic}_output_dequantized_q16_16",
                [4, 64],
            ),
            f"{_prefix}_weight_codes_i8": (
                f"{_semantic}_weight_signed_int8_codes",
                [64, 64],
            ),
            f"{_prefix}_weight_scale_q8_24": (
                f"{_semantic}_weight_per_output_q8_24_scale",
                [64],
            ),
        }
    )
TENSOR_CONTRACT.update(
    {
        "out_bias_q16_16": ("attention_output_projection_bias_q16_16", [64]),
        "attention_score_q8": (
            "causal_attention_score_q8_future_slots_zero",
            [4, 16, 4],
        ),
        "attention_maximum_q8": ("causal_attention_row_maximum_q8", [4, 16]),
        "attention_delta_q8": (
            "causal_attention_clamped_delta_q8_future_slots_zero",
            [4, 16, 4],
        ),
        "attention_exp_q1_20": (
            "causal_attention_exp_q1_20_future_slots_zero",
            [4, 16, 4],
        ),
        "attention_denominator_q1_20": (
            "causal_attention_probability_denominator_q1_20",
            [4, 16],
        ),
        "attention_numerator_q17_36": (
            "causal_attention_value_numerator_q17_36",
            [4, 16, 4],
        ),
        "attention_context_heads_q16_16": (
            "causal_attention_per_head_context_q16_16",
            [4, 16, 4],
        ),
        "attention_context_q16_16": (
            "causal_attention_concatenated_context_q16_16",
            [4, 64],
        ),
        "attention_exp_lut_q1_20": ("exact_attention_exp_lut_q1_20", [4096]),
        "attention_residual_q16_16": (
            "block_0_attention_residual_q16_16",
            [4, 64],
        ),
        "ln2_gamma_q16_16": ("block_0_ln2_gamma_q16_16", [64]),
        "ln2_beta_q16_16": ("block_0_ln2_beta_q16_16", [64]),
        "ln2_output_q16_16": ("block_0_ln2_output_q16_16", [4, 64]),
        "c_fc_input_codes_i8": ("c_fc_input_signed_int8_codes", [4, 64]),
        "c_fc_input_scale_q8_24": (
            "c_fc_input_per_channel_q8_24_scale",
            [64],
        ),
        "c_fc_input_q16_16": ("c_fc_input_dequantized_q16_16", [4, 64]),
    }
)

SLICE_CONTRACT = {
    "layer": 0,
    "rows": 4,
    "width": 64,
    "heads": 16,
    "head_width": 4,
    "causal_valid_key_counts": [1, 2, 3, 4],
    "observation_indices": {
        "q_qdq": [0, 6],
        "q_accumulator": 0,
        "k_qdq": [6, 12],
        "k_accumulator": 1,
        "v_qdq": [12, 18],
        "v_accumulator": 2,
        "out_qdq": [18, 24],
        "out_accumulator": 3,
        "ln1_nonlinear": 0,
        "attention_context_nonlinear": 1,
        "ln2_nonlinear": 2,
        "c_fc_input_qdq": [24, 27],
    },
}

ARITHMETIC_CONTRACT = {
    "value_format": "signed_q16.16_int64_tensor",
    "scale_format": "unsigned_q8.24_int64_tensor",
    "activation_codes": "signed_int8_saturated",
    "activation_qdq": "signed_int8_saturated_nearest_ties_away_from_zero",
    "gemv": "ascending_input_index_signed_int64_twos_complement_wrap",
    "post_weight_rescale": (
        "signed_magnitude_half_up_shift_32_plus_optional_q16.16_bias"
    ),
    "layer_norm": (
        "truncating_mean_variance_plus_42950_restoring_isqrt_"
        "truncating_division_q16.16_affine"
    ),
    "attention_score": "sum_q16.16_products_arithmetic_shift_right_24",
    "attention_exp": "delta_clamp_minus4096_to_0_q8_exact_zero_q1.20_lut",
    "attention_division": (
        "signed_half_denominator_correction_then_truncating_division"
    ),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("semantic", "shape", "dtype", "values")}


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


def _expected_identity() -> dict[str, str]:
    return {
        "contract_sha256": sha(CONTRACT),
        "package_manifest_sha256": sha(PACKAGE / "manifest.json"),
        "package_weights_sha256": sha(PACKAGE / "weights.bin"),
        "package_scales_sha256": sha(PACKAGE / "scales.bin"),
        "package_receipt_sha256": sha(PACKAGE / "receipt.json"),
        "model_config_sha256": sha(MODEL / "config.json"),
        "adapter_sha256": sha(ROOT / "TinyStories/model_adapter_exact_package.py"),
        "capture_sha256": sha(Path(__file__)),
        "successor_generation_receipt_sha256": sha(GENERATION_ARTIFACT),
        "linked_mlp_fixture_file_sha256": sha(MLP_FIXTURE),
    }


def _linked_mlp_authority(mlp: Mapping[str, Any]) -> dict[str, Any]:
    linked: dict[str, Any] = {
        "schema": mlp["schema"],
        "receipt_sha256": mlp["receipt_sha256"],
        "tensor_fixture_receipt_sha256": mlp["tensor_fixture_receipt_sha256"],
    }
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        record = mlp["tensors"][name]
        linked[name] = {
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
    return linked


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
            "linked_mlp",
        )
    } | {"tensor_receipts": tensor_receipts}


def _verify_observation_indices() -> None:
    expected_qdq = (
        "transformer.h.0.attn.attention.q_proj.input",
        "transformer.h.0.attn.attention.q_proj.output",
        "transformer.h.0.attn.attention.k_proj.input",
        "transformer.h.0.attn.attention.k_proj.output",
        "transformer.h.0.attn.attention.v_proj.input",
        "transformer.h.0.attn.attention.v_proj.output",
        "transformer.h.0.attn.attention.out_proj.input",
        "transformer.h.0.attn.attention.out_proj.output",
        "transformer.h.0.mlp.c_fc.input",
    )
    if QDQ_BOUNDARY_NAMES[:9] != expected_qdq:
        raise ValueError("exact adapter block-0 attention Q/DQ indices changed")
    if GEMV_NAMES[:4] != (
        "transformer.h.0.attn.attention.q_proj",
        "transformer.h.0.attn.attention.k_proj",
        "transformer.h.0.attn.attention.v_proj",
        "transformer.h.0.attn.attention.out_proj",
    ):
        raise ValueError("exact adapter block-0 attention accumulator indices changed")
    if NONLINEAR_BOUNDARY_NAMES[:3] != (
        "transformer.h.0.ln_1.output",
        "transformer.h.0.attn.attention.context",
        "transformer.h.0.ln_2.output",
    ):
        raise ValueError("exact adapter block-0 nonlinear indices changed")


def _trunc_divide(numerator: torch.Tensor, denominator: torch.Tensor) -> torch.Tensor:
    quotient = torch.div(
        torch.abs(numerator), torch.abs(denominator), rounding_mode="floor"
    )
    return torch.where((numerator < 0) != (denominator < 0), -quotient, quotient)


def _integer_sqrt(values: torch.Tensor) -> torch.Tensor:
    remainder = values.to(torch.int64)
    result = torch.zeros_like(remainder)
    bit = 1 << 62
    for _ in range(32):
        candidate = result + bit
        take = remainder >= candidate
        remainder = torch.where(take, remainder - candidate, remainder)
        result = torch.where(
            take,
            torch.bitwise_right_shift(result, 1) + bit,
            torch.bitwise_right_shift(result, 1),
        )
        bit >>= 2
    return result


def _fixed_layer_norm(
    values: torch.Tensor, gamma: torch.Tensor, beta: torch.Tensor
) -> torch.Tensor:
    rows = values.reshape(-1, values.shape[-1]).to(torch.int64)
    width = rows.shape[1]
    row_sum = torch.sum(rows, dim=1, keepdim=True, dtype=torch.int64)
    mean_magnitude = torch.div(torch.abs(row_sum), width, rounding_mode="floor")
    mean = torch.where(row_sum < 0, -mean_magnitude, mean_magnitude)
    deltas = rows - mean
    variance = torch.div(
        torch.sum(deltas * deltas, dim=1, keepdim=True, dtype=torch.int64),
        width,
        rounding_mode="floor",
    ) + 42950
    deviation = _integer_sqrt(variance)
    normalized = _trunc_divide(torch.bitwise_left_shift(deltas, Q_VALUE), deviation)
    return torch.bitwise_right_shift(normalized * gamma, Q_VALUE) + beta


def _post_weight_rescale(
    accumulator: torch.Tensor,
    weight_scale: torch.Tensor,
    bias: torch.Tensor | None = None,
) -> torch.Tensor:
    result = round_shift_signed(
        accumulator * weight_scale, 2 * Q_SCALE - Q_VALUE
    )
    return result if bias is None else result + bias


def _attention_trace(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    exp_lut: torch.Tensor,
) -> dict[str, torch.Tensor]:
    rows = int(query.shape[0])
    scores = torch.zeros((rows, 16, rows), dtype=torch.int64)
    maxima = torch.zeros((rows, 16), dtype=torch.int64)
    deltas = torch.zeros_like(scores)
    probabilities = torch.zeros_like(scores)
    denominators = torch.zeros((rows, 16), dtype=torch.int64)
    numerators = torch.zeros((rows, 16, 4), dtype=torch.int64)
    contexts = torch.zeros((rows, 16, 4), dtype=torch.int64)

    for position in range(rows):
        for head in range(16):
            columns = slice(head * 4, head * 4 + 4)
            products = query[position, columns] * key[: position + 1, columns]
            valid_scores = torch.bitwise_right_shift(
                torch.sum(products, dim=-1, dtype=torch.int64), 24
            )
            maximum = torch.amax(valid_scores)
            valid_deltas = torch.clamp(valid_scores - maximum, -4096, 0)
            table_index = torch.minimum(
                4096 + valid_deltas, torch.full_like(valid_deltas, 4095)
            )
            valid_probabilities = torch.where(
                valid_deltas == 0,
                torch.full_like(valid_deltas, 1 << 20),
                exp_lut[table_index],
            )
            denominator = torch.sum(valid_probabilities, dtype=torch.int64)
            numerator = torch.sum(
                valid_probabilities.unsqueeze(-1)
                * value[: position + 1, columns],
                dim=0,
                dtype=torch.int64,
            )
            half = torch.div(denominator, 2, rounding_mode="floor")
            correction = torch.where(numerator < 0, -half, half)
            context = _trunc_divide(numerator + correction, denominator)

            scores[position, head, : position + 1] = valid_scores
            maxima[position, head] = maximum
            deltas[position, head, : position + 1] = valid_deltas
            probabilities[position, head, : position + 1] = valid_probabilities
            denominators[position, head] = denominator
            numerators[position, head] = numerator
            contexts[position, head] = context

    return {
        "attention_score_q8": scores,
        "attention_maximum_q8": maxima,
        "attention_delta_q8": deltas,
        "attention_exp_q1_20": probabilities,
        "attention_denominator_q1_20": denominators,
        "attention_numerator_q17_36": numerators,
        "attention_context_heads_q16_16": contexts,
        "attention_context_q16_16": contexts.reshape(rows, 64),
    }


def _projection_tensors(
    prefix: str,
    observations: tuple[torch.Tensor, ...],
    accumulator: torch.Tensor,
    weight_codes: torch.Tensor,
    weight_scale: torch.Tensor,
    bias: torch.Tensor | None,
) -> dict[str, torch.Tensor]:
    (
        input_codes,
        input_scale,
        input_q16,
        output_codes,
        output_scale,
        output_q16,
    ) = observations
    post = _post_weight_rescale(accumulator, weight_scale, bias)
    return {
        f"{prefix}_input_codes_i8": input_codes,
        f"{prefix}_input_scale_q8_24": input_scale,
        f"{prefix}_input_q16_16": input_q16,
        f"{prefix}_accumulator_i64": accumulator,
        f"{prefix}_post_weight_rescale_bias_q16_16": post,
        f"{prefix}_output_codes_i8": output_codes,
        f"{prefix}_output_scale_q8_24": output_scale,
        f"{prefix}_output_q16_16": output_q16,
        f"{prefix}_weight_codes_i8": weight_codes,
        f"{prefix}_weight_scale_q8_24": weight_scale,
    }


def capture() -> dict[str, Any]:
    _verify_observation_indices()
    mlp = capture_mlp.verify_fixture(MLP_FIXTURE)
    bundle = load_successor_exact_model(CONTRACT, PACKAGE, MODEL, GENERATION_ARTIFACT)
    prompt = torch.tensor(
        [bundle.contract["reference"]["prompt_tokens"]], dtype=torch.int64
    )
    if prompt[0].tolist() != PROMPT_TOKENS:
        raise ValueError("exact adapter frozen prompt changed")

    with torch.no_grad():
        _, block_zero, qdq, accumulators, nonlinear = bundle.model._execute(prompt)
        positions = torch.arange(4, dtype=torch.int64).unsqueeze(0)
        block_input = (
            bundle.model._embedding("token_embedding.weight", prompt)
            + bundle.model._embedding("position_embedding.weight", positions)
        ).reshape(4, 64)

    if len(qdq) < 27 or len(accumulators) < 4 or len(nonlinear) < 3:
        raise ValueError("exact adapter block-0 attention trace is incomplete")

    ln1_gamma = bundle.model._buffer("parameter", "blocks.0.ln1.weight")
    ln1_beta = bundle.model._buffer("parameter", "blocks.0.ln1.bias")
    ln1_output = nonlinear[0].reshape(4, 64)
    ln1_replay = _fixed_layer_norm(block_input, ln1_gamma, ln1_beta)
    if not torch.equal(ln1_replay, ln1_output):
        raise ValueError("captured exact ln1 observation does not replay")

    projection_specs = (
        ("q", 0, 0, "blocks.0.attn.q.weight", None),
        ("k", 6, 1, "blocks.0.attn.k.weight", None),
        ("v", 12, 2, "blocks.0.attn.v.weight", None),
        ("out", 18, 3, "blocks.0.attn.out.weight", "blocks.0.attn.out.bias"),
    )
    tensors: dict[str, torch.Tensor] = {
        "block_input_q16_16": block_input,
        "ln1_gamma_q16_16": ln1_gamma,
        "ln1_beta_q16_16": ln1_beta,
        "ln1_output_q16_16": ln1_output,
    }
    for prefix, qdq_start, accumulator_index, weight_name, bias_name in projection_specs:
        weight_codes = bundle.model._buffer("code", weight_name)
        weight_scale = bundle.model._buffer("scale", weight_name)
        bias = (
            None
            if bias_name is None
            else bundle.model._buffer("parameter", bias_name)
        )
        tensors.update(
            _projection_tensors(
                prefix,
                qdq[qdq_start : qdq_start + 6],
                accumulators[accumulator_index],
                weight_codes,
                weight_scale,
                bias,
            )
        )
    tensors["out_bias_q16_16"] = bundle.model._buffer(
        "parameter", "blocks.0.attn.out.bias"
    )

    attention_trace = _attention_trace(
        tensors["q_output_q16_16"],
        tensors["k_output_q16_16"],
        tensors["v_output_q16_16"],
        bundle.model._exp_lut,
    )
    adapter_context = nonlinear[1].reshape(4, 64)
    if not torch.equal(attention_trace["attention_context_q16_16"], adapter_context):
        raise ValueError("captured exact causal attention observation does not replay")
    tensors.update(attention_trace)
    tensors["attention_exp_lut_q1_20"] = bundle.model._exp_lut

    attention_residual = block_input + tensors["out_output_q16_16"]
    ln2_gamma = bundle.model._buffer("parameter", "blocks.0.ln2.weight")
    ln2_beta = bundle.model._buffer("parameter", "blocks.0.ln2.bias")
    ln2_output = nonlinear[2].reshape(4, 64)
    if not torch.equal(
        _fixed_layer_norm(attention_residual, ln2_gamma, ln2_beta), ln2_output
    ):
        raise ValueError("captured exact ln2 observation does not replay")
    tensors.update(
        {
            "attention_residual_q16_16": attention_residual,
            "ln2_gamma_q16_16": ln2_gamma,
            "ln2_beta_q16_16": ln2_beta,
            "ln2_output_q16_16": ln2_output,
            "c_fc_input_codes_i8": qdq[24],
            "c_fc_input_scale_q8_24": qdq[25],
            "c_fc_input_q16_16": qdq[26],
        }
    )

    block_zero_checks = (
        ("block input", block_input[-1], block_zero[0]),
        ("ln1", ln1_output[-1], block_zero[1]),
        ("query", tensors["q_output_q16_16"][-1].reshape(16, 4), block_zero[2]),
        ("key", tensors["k_output_q16_16"][-1].reshape(16, 4), block_zero[3]),
        ("value", tensors["v_output_q16_16"][-1].reshape(16, 4), block_zero[4]),
        ("attention output", tensors["out_output_q16_16"][-1], block_zero[5]),
        ("attention residual", attention_residual[-1], block_zero[6]),
        ("ln2", ln2_output[-1], block_zero[7]),
    )
    for semantic, actual, expected in block_zero_checks:
        if not torch.equal(actual, expected):
            raise ValueError(f"captured four-row {semantic} differs from adapter checkpoint")

    c_fc_codes, c_fc_q16 = activation_qdq(
        ln2_output, tensors["c_fc_input_scale_q8_24"]
    )
    if not torch.equal(c_fc_codes, tensors["c_fc_input_codes_i8"]):
        raise ValueError("captured c_fc input code Q/DQ does not replay")
    if not torch.equal(c_fc_q16, tensors["c_fc_input_q16_16"]):
        raise ValueError("captured c_fc input value Q/DQ does not replay")
    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        if tensors[name].tolist() != mlp["tensors"][name]["values"]:
            raise ValueError(f"attention-to-MLP linked boundary mismatch: {name}")

    if set(tensors) != set(TENSOR_CONTRACT):
        missing = sorted(set(TENSOR_CONTRACT) - set(tensors))
        extra = sorted(set(tensors) - set(TENSOR_CONTRACT))
        raise ValueError(f"attention tensor contract mismatch: missing={missing}, extra={extra}")
    records = {
        name: tensor_record(tensor, TENSOR_CONTRACT[name][0])
        for name, tensor in tensors.items()
    }
    value: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "authenticated_frozen_prompt_attention_fixture",
        "identity": _expected_identity(),
        "prompt_tokens": prompt[0].tolist(),
        "slice": SLICE_CONTRACT,
        "arithmetic": ARITHMETIC_CONTRACT,
        "linked_mlp": _linked_mlp_authority(mlp),
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
        "linked_mlp",
        "tensors",
        "tensor_fixture_receipt_sha256",
        "receipt_sha256",
    }
    if set(value) != required_top_level:
        raise ValueError("fixture top-level field set mismatch")
    if value["schema"] != SCHEMA:
        raise ValueError("fixture schema mismatch")
    if value["status"] != "authenticated_frozen_prompt_attention_fixture":
        raise ValueError("fixture authentication status mismatch")
    if value["prompt_tokens"] != PROMPT_TOKENS:
        raise ValueError("fixture frozen prompt mismatch")
    if value["slice"] != SLICE_CONTRACT:
        raise ValueError("fixture attention slice contract mismatch")
    if value["arithmetic"] != ARITHMETIC_CONTRACT:
        raise ValueError("fixture arithmetic contract mismatch")
    if value["identity"] != _expected_identity():
        raise ValueError("fixture source identity mismatch")

    mlp = capture_mlp.verify_fixture(MLP_FIXTURE)
    expected_link = _linked_mlp_authority(mlp)
    if value["linked_mlp"] != expected_link:
        raise ValueError("fixture linked MLP authority mismatch")

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

    for name in (
        "c_fc_input_codes_i8",
        "c_fc_input_scale_q8_24",
        "c_fc_input_q16_16",
    ):
        if tensors[name]["values"] != mlp["tensors"][name]["values"]:
            raise ValueError(f"fixture linked MLP tensor mismatch: {name}")
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


def _require_equal(actual: torch.Tensor, expected: torch.Tensor, label: str) -> None:
    if not torch.equal(actual, expected):
        raise ValueError(f"fixture {label} replay mismatch")


def _verify_projection(
    value: Mapping[str, Any],
    prefix: str,
    source_q16: torch.Tensor,
    bias: torch.Tensor | None,
) -> torch.Tensor:
    input_scale = _tensor(value, f"{prefix}_input_scale_q8_24")
    input_codes, input_q16 = activation_qdq(source_q16, input_scale)
    _require_equal(input_codes, _tensor(value, f"{prefix}_input_codes_i8"), f"{prefix} input code Q/DQ")
    _require_equal(input_q16, _tensor(value, f"{prefix}_input_q16_16"), f"{prefix} input value Q/DQ")
    accumulator = serial_gemv(
        input_codes * input_scale, _tensor(value, f"{prefix}_weight_codes_i8")
    )
    _require_equal(accumulator, _tensor(value, f"{prefix}_accumulator_i64"), f"{prefix} exact GEMV")
    post = _post_weight_rescale(
        accumulator, _tensor(value, f"{prefix}_weight_scale_q8_24"), bias
    )
    _require_equal(
        post,
        _tensor(value, f"{prefix}_post_weight_rescale_bias_q16_16"),
        f"{prefix} post-weight rescale",
    )
    output_codes, output_q16 = activation_qdq(
        post, _tensor(value, f"{prefix}_output_scale_q8_24")
    )
    _require_equal(output_codes, _tensor(value, f"{prefix}_output_codes_i8"), f"{prefix} output code Q/DQ")
    _require_equal(output_q16, _tensor(value, f"{prefix}_output_q16_16"), f"{prefix} output value Q/DQ")
    return output_q16


def verify_fixed_point_replay(path: Path) -> None:
    value = verify_fixture(path)
    block_input = _tensor(value, "block_input_q16_16")
    ln1 = _fixed_layer_norm(
        block_input,
        _tensor(value, "ln1_gamma_q16_16"),
        _tensor(value, "ln1_beta_q16_16"),
    )
    _require_equal(ln1, _tensor(value, "ln1_output_q16_16"), "ln1")

    query = _verify_projection(value, "q", ln1, None)
    key = _verify_projection(value, "k", ln1, None)
    projected_value = _verify_projection(value, "v", ln1, None)
    trace = _attention_trace(
        query, key, projected_value, _tensor(value, "attention_exp_lut_q1_20")
    )
    for name, tensor in trace.items():
        _require_equal(tensor, _tensor(value, name), name)

    attention = _verify_projection(
        value,
        "out",
        trace["attention_context_q16_16"],
        _tensor(value, "out_bias_q16_16"),
    )
    residual = block_input + attention
    _require_equal(
        residual,
        _tensor(value, "attention_residual_q16_16"),
        "attention residual",
    )
    ln2 = _fixed_layer_norm(
        residual,
        _tensor(value, "ln2_gamma_q16_16"),
        _tensor(value, "ln2_beta_q16_16"),
    )
    _require_equal(ln2, _tensor(value, "ln2_output_q16_16"), "ln2")
    c_fc_codes, c_fc_q16 = activation_qdq(
        ln2, _tensor(value, "c_fc_input_scale_q8_24")
    )
    _require_equal(
        c_fc_codes, _tensor(value, "c_fc_input_codes_i8"), "c_fc input codes"
    )
    _require_equal(
        c_fc_q16, _tensor(value, "c_fc_input_q16_16"), "c_fc input Q/DQ"
    )


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
