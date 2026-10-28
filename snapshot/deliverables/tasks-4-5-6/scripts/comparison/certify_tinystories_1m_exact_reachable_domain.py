#!/usr/bin/env python3
"""Prove package-reachable fixed runtime/RTL equivalence for exact TinyStories.

The older LayerNorm receipt correctly rejects equivalence over arbitrary signed
32-bit port values.  This certificate derives a smaller conservative domain
from the authenticated package: embedding extrema, Q/DQ-clamped projection
extrema, and residual topology.  Every width inequality must close or the
result is an ``identity_frontier`` with the first failing inequality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import torch


SCHEMA = "tinystories-1m-exact-reachable-domain-v1"
CONTRACT_SHA256 = "859fe3095a4842e413ee99466f5dc63d5420d0e890a3dce0cf7a52e3bd2d1d3c"
AUDIT_FILE_SHA256 = "3cf8a5b9db8acf0ca04e92277c0f9f07c81900a4c754626183bd1d22063616bd"
PROFILE_SHA256 = "f3fa88e8af4982a0e189a3887cd256d207d4c0a891ec587ab3b11b069785c9a6"
SOFTMAX_SHA256 = "d6f5c89799a7e66c825a003c07680ea8334b2f1264fb0ef4ccf942b4ec20f1b1"
LAYERNORM_RECEIPT = Path("artifacts/reference/tinystories-1m-layernorm-semantics.json")
GELU_RECEIPT = Path("artifacts/comparison/tinystories-1m-gelu-equivalence-2026-08-29.json")
GELU_ADAPTER = Path("rtl/llm2fpga_gelu_q16_tensor.sv")
LAYERNORM_RECEIPT_SHA256 = "e035dc5835a3908fa687281089e75d1a9dac0f846f84950211ba2681f1259006"
GELU_RECEIPT_SHA256 = "2edeecada4d0abc915d877b2093dd78bdf06e57872a1b0b35cb6102f728b1f3e"
GELU_ADAPTER_SHA256 = "efeb6d00d6554976dc22a0cd6017c49d04a346cf8407e195d277f3be9d83c24b"
REFERENCE_REVISION = "df1fc45b2ffcb26fddc19cfd57621e7eedf6153f"
SEMANTIC_SOURCE_PATHS = (
    "tinystories/hardware_reference.py",
    "tinystories/rtl_memories.py",
    "fpga/rtl/gptneo_layernorm.sv",
    "fpga/rtl/gptneo_gelu.sv",
    "fpga/rtl/gptneo_attention.sv",
    "fpga/rtl/gptneo_iterative_divider.sv",
)
Q_VALUE = 16
Q_SCALE = 24
EPSILON_Q32 = 42950


class ReachableDomainError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ReachableDomainError(code, message)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def certificate_sha256(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "certificate_sha256"})


def summarize_ordered_values(values: list[int], module: str, term: str,
                             first_failure_witness: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compact a fully computed ordered vector without losing content or witnesses."""

    _require(bool(values) and all(isinstance(value, int) and not isinstance(value, bool)
                                  for value in values),
             "summary_input_malformed", f"{module}:{term}")
    minimum = min(values)
    maximum = max(values)
    worst_index = max(range(len(values)), key=lambda index: abs(values[index]))
    worst = values[worst_index]
    return {
        "ordered_values_sha256": canonical_sha256(values),
        "element_count": len(values),
        "signed_minimum": {"value": minimum, "output_index": values.index(minimum)},
        "signed_maximum": {"value": maximum, "output_index": values.index(maximum)},
        "absolute_maximum": {
            "value": worst, "magnitude": abs(worst), "output_index": worst_index,
        },
        "worst_case_witness": {
            "module": module, "output": worst_index, "term": term,
            "value": worst, "absolute_value": abs(worst),
        },
        "first_failure_witness": (
            dict(first_failure_witness) if first_failure_witness is not None else None
        ),
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "invalid_json", str(path))
    return value


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ReachableDomainError("pinned_source_unavailable", " ".join(args)) from error


def _git_text(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode().strip()


def materialize_semantic_sources(contract_path: Path, audit_path: Path,
                                 reference_root: Path) -> dict[str, dict[str, Any]]:
    """Read every semantic authority from the authenticated immutable Git tree."""

    contract_path, audit_path, reference_root = map(
        Path, (contract_path, audit_path, reference_root)
    )
    _require(contract_path.is_file() and sha256_file(contract_path) == CONTRACT_SHA256,
             "authority_identity_mismatch", str(contract_path))
    _require(audit_path.is_file() and sha256_file(audit_path) == AUDIT_FILE_SHA256,
             "authority_identity_mismatch", str(audit_path))
    contract, audit = _load(contract_path), _load(audit_path)
    _require(_git_text(reference_root, "rev-parse", "--show-toplevel")
             == str(reference_root.resolve()),
             "reference_root_mismatch", str(reference_root))
    _require(_git_text(reference_root, "rev-parse", f"{REFERENCE_REVISION}^{{commit}}")
             == REFERENCE_REVISION,
             "source_revision_mismatch", REFERENCE_REVISION)
    closure = contract["deployed_profile"]["sources"]
    audit_closure = audit["source"]["pinned_semantic_source_closure"]
    materialized: dict[str, dict[str, Any]] = {}
    for name in SEMANTIC_SOURCE_PATHS:
        expected = closure.get(name)
        _require(expected == audit_closure.get(name), "source_identity_mismatch", name)
        blob = _git_text(reference_root, "rev-parse", f"{REFERENCE_REVISION}:{name}")
        payload = _git_bytes(reference_root, "cat-file", "blob", blob)
        actual = {"git_blob_sha1": blob, "sha256": hashlib.sha256(payload).hexdigest()}
        _require(actual == expected, "source_identity_mismatch", name)
        materialized[name] = {**actual, "payload": payload}
    return materialized


def _trunc(numerator: int, denominator: int) -> int:
    quotient = abs(numerator) // abs(denominator)
    return -quotient if (numerator < 0) != (denominator < 0) else quotient


def _wrap(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _round_shift(value: int, shift: int) -> int:
    magnitude = abs(value)
    rounded = (magnitude + (1 << (shift - 1))) >> shift
    return -rounded if value < 0 else rounded


def _normalized_abs_bound(delta_abs_bound: int) -> int:
    """Exhaust the scalar relaxation S >= delta_i^2 for a rigorous bound."""

    maximum = 0
    for delta in range(1, delta_abs_bound + 1):
        denominator = math.isqrt(delta * delta // 64 + EPSILON_Q32)
        maximum = max(maximum, delta * (1 << Q_VALUE) // denominator)
    return maximum


def _runtime_layer_norm(values: list[int], gamma: list[int], beta: list[int]) -> list[int]:
    mean = _trunc(sum(values), 64)
    deltas = [value - mean for value in values]
    variance = sum(delta * delta for delta in deltas) // 64 + EPSILON_Q32
    deviation = math.isqrt(variance)
    normalized = [_trunc(delta << Q_VALUE, deviation) for delta in deltas]
    return [((value * scale) >> Q_VALUE) + offset
            for value, scale, offset in zip(normalized, gamma, beta, strict=True)]


def _rtl_layer_norm(values: list[int], gamma: list[int], beta: list[int]) -> list[int]:
    total = 0
    for value in values:
        total = _wrap(total + value, 64)
    mean = _trunc(total, 64)
    square_sum = 0
    deltas = []
    for value in values:
        delta = _wrap(value - mean, 33)
        deltas.append(delta)
        square_sum = (square_sum + abs(delta) * abs(delta)) & ((1 << 72) - 1)
    variance = (square_sum // 64 + EPSILON_Q32) & ((1 << 64) - 1)
    deviation = math.isqrt(variance)
    normalized = [_wrap(_trunc(delta << Q_VALUE, deviation), 32) for delta in deltas]
    return [_wrap(((value * scale) >> Q_VALUE) + offset, 32)
            for value, scale, offset in zip(normalized, gamma, beta, strict=True)]


def prove_layernorm_interval(name: str, lower: list[int], upper: list[int],
                             gamma: list[int], beta: list[int]) -> dict[str, Any]:
    _require(len(lower) == len(upper) == len(gamma) == len(beta) == 64,
             "layernorm_shape_mismatch", name)
    sum_lower = sum(lower)
    sum_upper = sum(upper)
    mean_lower = _trunc(sum_lower, 64)
    mean_upper = _trunc(sum_upper, 64)
    delta_lower = [value - mean_upper for value in lower]
    delta_upper = [value - mean_lower for value in upper]
    delta_abs = max(max(abs(value) for value in delta_lower),
                    max(abs(value) for value in delta_upper))
    square_each = [max(lo * lo, hi * hi) for lo, hi in zip(delta_lower, delta_upper, strict=True)]
    square_sum = sum(square_each)
    variance = square_sum // 64 + EPSILON_Q32
    reduction_domain_closes = (
        max(square_each) < (1 << 63)
        and square_sum < (1 << 63)
        and square_sum < (1 << 72)
        and variance < (1 << 63)
    )
    # Do not perform an enormous scalar-domain enumeration after a prior width
    # inequality has already made the call an identity frontier.
    normalized = _normalized_abs_bound(delta_abs) if reduction_domain_closes else (1 << 31)
    affine_product = normalized * max(abs(value) for value in gamma)
    affine_output = 0
    for scale, offset in zip(gamma, beta, strict=True):
        product = normalized * abs(scale)
        affine_output = max(affine_output, abs((-product >> Q_VALUE) + offset),
                            abs((product >> Q_VALUE) + offset))
    inequalities = {
        "input_fits_signed_int32": min(lower) >= -(1 << 31) and max(upper) < (1 << 31),
        "runtime_sum_fits_signed_int64": max(abs(sum_lower), abs(sum_upper)) < (1 << 63),
        "rtl_delta_fits_signed_33": delta_abs < (1 << 32),
        "runtime_each_square_fits_signed_int64": max(square_each) < (1 << 63),
        "runtime_square_sum_fits_signed_int64": square_sum < (1 << 63),
        "rtl_square_sum_fits_unsigned_72": square_sum < (1 << 72),
        "variance_fits_runtime_signed_int64": variance < (1 << 63),
        "variance_fits_rtl_unsigned_64": variance < (1 << 64),
        "divider_numerator_fits_signed_96": delta_abs << Q_VALUE < (1 << 95),
        "normalized_quotient_fits_signed_int32": normalized < (1 << 31),
        "affine_product_fits_signed_int64": affine_product < (1 << 63),
        "affine_output_fits_signed_int32": affine_output < (1 << 31),
    }
    ordered = list(inequalities)
    failing = next((key for key in ordered if not inequalities[key]), None)
    witnesses = []
    for label, values in (
        ("alternating_lower_upper", [lower[i] if i % 2 == 0 else upper[i] for i in range(64)]),
        ("alternating_upper_lower", [upper[i] if i % 2 == 0 else lower[i] for i in range(64)]),
    ):
        runtime = _runtime_layer_norm(values, gamma, beta) if failing is None else None
        rtl = _rtl_layer_norm(values, gamma, beta) if failing is None else None
        witnesses.append({
            "pattern": label,
            "input_sha256": canonical_sha256(values),
            "runtime_sha256": canonical_sha256(runtime) if runtime is not None else None,
            "rtl_sha256": canonical_sha256(rtl) if rtl is not None else None,
            "matched": runtime == rtl if runtime is not None else False,
        })
    return {
        "name": name,
        "status": "proven" if failing is None else "identity_frontier",
        "failing_inequality": failing,
        "input_bounds_q16_16": {"lower_by_channel": lower, "upper_by_channel": upper},
        "parameter_identity": {
            "gamma_sha256": canonical_sha256(gamma), "beta_sha256": canonical_sha256(beta),
            "gamma_abs_max": max(abs(value) for value in gamma),
            "beta_abs_max": max(abs(value) for value in beta),
        },
        "proof": {
            "sum_lower": sum_lower, "sum_upper": sum_upper,
            "delta_abs_bound": delta_abs,
            "each_square_abs_bound": max(square_each),
            "square_sum_abs_bound": square_sum,
            "variance_abs_bound": variance,
            "normalized_abs_bound": normalized,
            "affine_product_abs_bound": affine_product,
            "affine_output_abs_bound": affine_output,
            "inequalities": inequalities,
        },
        "edge_witnesses": {
            "status": "runtime_rtl_matched" if all(item["matched"] for item in witnesses) else "not_compared",
            "vectors": witnesses,
        },
    }


def _read_package(package: Path) -> tuple[dict[str, Any], bytes, bytes]:
    manifest = _load(package / "manifest.json")
    return manifest, (package / "weights.bin").read_bytes(), (package / "scales.bin").read_bytes()


def _q24_scales(descriptor: Mapping[str, Any], scales_image: bytes) -> torch.Tensor:
    offset = int(descriptor["scale_offset"])
    raw = scales_image[offset:offset + int(descriptor["scale_nbytes"])]
    return torch.round(
        torch.frombuffer(bytearray(raw), dtype=torch.float32).to(torch.float64) * (1 << Q_SCALE)
    ).to(torch.int64)


def _parameter_q16(name: str, manifest: Mapping[str, Any], weights: bytes) -> list[int]:
    descriptor = manifest["tensors"][name]
    offset = int(descriptor["offset"])
    raw = weights[offset:offset + int(descriptor["nbytes"])]
    values = torch.frombuffer(bytearray(raw), dtype=torch.float32).to(torch.float64)
    rounded = torch.round(values * (1 << Q_VALUE))
    _require(bool(torch.isfinite(rounded).all())
             and bool(((rounded >= -(1 << 31)) & (rounded < (1 << 31))).all()),
             "parameter_q16_range_failure", name)
    return rounded.to(torch.int64).tolist()


def _embedding_bounds(name: str, rows: int, manifest: Mapping[str, Any], weights: bytes,
                      scales_image: bytes) -> tuple[list[int], list[int]]:
    descriptor = manifest["tensors"][name]
    shape = tuple(descriptor["logical_shape"])
    offset = int(descriptor["offset"])
    raw = weights[offset:offset + int(descriptor["nbytes"])]
    codes = torch.frombuffer(bytearray(raw), dtype=torch.int8).reshape(shape)[:rows].to(torch.int64)
    scales = _q24_scales(descriptor, scales_image)[:rows]
    products = codes * scales[:, None]
    magnitude = (products.abs() + 128) >> 8
    values = torch.where(products < 0, -magnitude, magnitude)
    return values.amin(dim=0).tolist(), values.amax(dim=0).tolist()


def _activation_bounds(name: str, manifest: Mapping[str, Any]) -> tuple[list[int], list[int]]:
    scales = torch.round(
        torch.tensor(manifest["activation_scales"][name], dtype=torch.float64) * (1 << Q_SCALE)
    ).to(torch.int64).tolist()
    return ([_round_shift(-128 * value, 8) for value in scales],
            [_round_shift(127 * value, 8) for value in scales])


def _runtime_gelu_lut() -> list[int]:
    values = []
    for index in range(8192):
        value = -8.0 + index / 512.0
        result = 0.5 * value * (
            1.0 + math.tanh(math.sqrt(2.0 / math.pi) * (value + 0.044715 * value ** 3))
        )
        values.append(max(-32768, min(32767, round(result * 4096.0))))
    return values


def _runtime_exp_lut() -> list[int]:
    return [round(math.exp((index - 4096) / 256.0) * (1 << 20))
            for index in range(4096)]


def _signed(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _materialize_rtl_lut_payloads(
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[bytes, bytes]:
    source = sources["tinystories/rtl_memories.py"]["payload"]
    namespace: dict[str, Any] = {"__name__": "authenticated_rtl_memories"}
    exec(compile(source, "authenticated:tinystories/rtl_memories.py", "exec"), namespace)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        gelu_path = namespace["write_gelu_lut"](directory)
        exp_path = namespace["write_exp_lut"](directory)
        return gelu_path.read_bytes(), exp_path.read_bytes()


def materialize_luts(
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[list[int], list[int], tuple[list[int], list[int]]]:
    gelu_payload, exp_payload = _materialize_rtl_lut_payloads(sources)
    rtl_gelu = [_signed(int(line, 16), 16) for line in gelu_payload.decode().splitlines()]
    rtl_exp = [int(line, 16) for line in exp_payload.decode().splitlines()]
    _require(len(rtl_gelu) == 8192 and len(rtl_exp) == 4096,
             "rtl_lut_materialization_failure", "unexpected LUT length")
    return _runtime_gelu_lut(), rtl_gelu, (_runtime_exp_lut(), rtl_exp)


def _runtime_gelu_q12(value: int, lut: list[int]) -> int:
    biased = value + 32768
    index, fraction = biased >> 3, biased & 7
    upper = min(index + 1, 8191)
    return lut[index] + ((lut[upper] - lut[index]) * fraction >> 3)


def _rtl_gelu_q12(value: int, lut: list[int]) -> int:
    in_data = _signed(value, 16)
    biased = ((in_data & 0xffff) + 0x8000) & 0xffff
    index, fraction = biased >> 3, biased & 7
    lower = _signed(lut[index], 16)
    upper = _signed(lut[index if index == 8191 else index + 1], 16)
    difference = _signed(upper - lower, 17)
    step = _signed(difference * fraction, 20)
    return _signed(lower + (step >> 3), 16)


def prove_gelu_semantics(runtime_lut: list[int], rtl_lut: list[int]) -> dict[str, Any]:
    _require(len(runtime_lut) == len(rtl_lut) == 8192,
             "gelu_lut_shape_mismatch", "expected 8192 entries")
    mismatch = None
    passed = 0
    for value in range(-32768, 32768):
        runtime = _runtime_gelu_q12(value, runtime_lut)
        rtl = _rtl_gelu_q12(value, rtl_lut)
        if runtime != rtl:
            mismatch = {"input_q4_12": value, "runtime_q4_12": runtime, "rtl_q4_12": rtl}
            break
        passed += 1
    runtime_values_hash = canonical_sha256(runtime_lut)
    rtl_values_hash = canonical_sha256(rtl_lut)
    return {
        "status": ("exhaustive_runtime_rtl_equivalent" if mismatch is None
                   else "identity_frontier"),
        "input_domain": {
            "format": "signed_q4.12_int16", "minimum": -32768,
            "maximum": 32767, "count": 65536,
        },
        "equations": {
            "runtime": "index=(x+32768)>>3; fraction=(x+32768)&7; lower+((upper-lower)*fraction>>3)",
            "rtl": "signed16 LUT; signed17 difference; signed20 product; arithmetic_shift_right_3; signed16 output",
        },
        "runtime_lut": {"entry_count": 8192, "values_sha256": runtime_values_hash},
        "rtl_lut": {
            "entry_count": 8192,
            "values_sha256": rtl_values_hash,
            "mem_sha256": hashlib.sha256(
                "".join(f"{value & 0xffff:04x}\n" for value in rtl_lut).encode()
            ).hexdigest(),
        },
        "comparison": {"pass_count": passed, "mismatch_witness": mismatch},
    }


def _runtime_exp(delta: int, lut: list[int]) -> int:
    clipped = max(-4096, min(0, delta))
    index = min(4096 + clipped, 4095)
    return 1 << 20 if clipped == 0 else lut[index]


def _rtl_exp(delta: int, lut: list[int]) -> int:
    if delta < -4096:
        index = 0
    elif delta >= 0:
        index = 4096
    else:
        index = 4096 + delta
    return 1 << 20 if index == 4096 else lut[index]


def prove_exp_semantics(runtime_lut: list[int], rtl_lut: list[int]) -> dict[str, Any]:
    _require(len(runtime_lut) == len(rtl_lut) == 4096,
             "exp_lut_shape_mismatch", "expected 4096 entries")
    representatives = [-2147483648, -4097, 1, 2147483647]
    domain = list(range(-4096, 1)) + representatives
    mismatch = None
    passed = 0
    for delta in domain:
        runtime, rtl = _runtime_exp(delta, runtime_lut), _rtl_exp(delta, rtl_lut)
        if runtime != rtl:
            mismatch = {"delta": delta, "runtime_q0_20": runtime, "rtl_q0_20": rtl}
            break
        passed += 1
    return {
        "status": ("exhaustive_effective_domain_equivalent" if mismatch is None
                   else "identity_frontier"),
        "effective_delta_domain": [-4096, 0],
        "equations": {
            "runtime": "clip(delta,-4096,0); index=min(4096+delta,4095); delta_zero=>2^20",
            "rtl": "delta<-4096=>0; delta>=0=>4096; else 4096+delta; index4096=>2^20",
        },
        "runtime_lut": {"entry_count": 4096, "values_sha256": canonical_sha256(runtime_lut)},
        "rtl_lut": {
            "entry_count": 4096,
            "values_sha256": canonical_sha256(rtl_lut),
            "mem_sha256": hashlib.sha256(
                "".join(f"{value:06x}\n" for value in rtl_lut).encode()
            ).hexdigest(),
        },
        "comparison": {
            "pass_count": passed, "mismatch_witness": mismatch,
            "outside_clamp_representatives": representatives,
        },
    }


def _gelu_proof(layer: int, manifest: Mapping[str, Any]) -> dict[str, Any]:
    name = f"transformer.h.{layer}.mlp.c_fc.output"
    lower, upper = _activation_bounds(name, manifest)
    input_abs = max(max(abs(value) for value in lower), max(abs(value) for value in upper))
    inequalities = {
        "input_fits_signed_int32": input_abs < (1 << 31),
        "signed_negation_and_rounding_add_are_safe": input_abs + 8 < (1 << 31),
        "q4_12_clamp_fits_signed_16": True,
        "lut_difference_fits_signed_17": 65535 < (1 << 16),
        "interpolation_product_fits_signed_20": 65535 * 7 < (1 << 19),
        "q16_output_fits_signed_int32": 32768 << 4 < (1 << 31),
    }
    return {
        "name": f"transformer.h.{layer}.mlp.gelu",
        "input_boundary": name,
        "input_bounds_q16_16": {"lower_by_channel": lower, "upper_by_channel": upper},
        "proof": {"input_abs_bound": input_abs, "inequalities": inequalities,
                  "all": all(inequalities.values())},
    }


def _rtl_restoring_divide(numerator: int, denominator: int) -> int:
    """Independent cycle-equivalent model of gptneo_iterative_divider.sv."""

    numerator = _signed(numerator, 96)
    denominator &= (1 << 64) - 1
    if denominator == 0:
        return 0
    negative = numerator < 0
    dividend = (-numerator if negative else numerator) & ((1 << 96) - 1)
    remainder = 0
    quotient = 0
    for bit in range(95, -1, -1):
        remainder = ((remainder & ((1 << 64) - 1)) << 1) | ((dividend >> bit) & 1)
        if remainder >= denominator:
            remainder -= denominator
            quotient |= 1 << bit
    low = _signed(quotient & 0xffffffff, 32)
    return _signed(-low if negative else low, 32)


def prove_divider_semantics(numerator_abs_bound: int, denominator_min: int,
                            denominator_max: int) -> dict[str, Any]:
    _require(0 <= numerator_abs_bound < (1 << 95),
             "divider_domain_mismatch", "numerator must fit signed 96")
    _require(0 < denominator_min <= denominator_max < (1 << 64),
             "divider_domain_mismatch", "positive denominator interval required")
    numerators = {
        -numerator_abs_bound, -max(0, numerator_abs_bound - 1),
        -denominator_max, -(denominator_max // 2), -denominator_min,
        -(denominator_min // 2), -2, -1, 0, 1, 2,
        denominator_min // 2, denominator_min, denominator_max // 2,
        denominator_max, max(0, numerator_abs_bound - 1), numerator_abs_bound,
    }
    denominators = {
        0, denominator_min, denominator_min + 1,
        (denominator_min + denominator_max) // 2,
        denominator_max - 1, denominator_max,
    }
    mismatch = None
    passed = 0
    for numerator in sorted(numerators):
        for denominator in sorted(denominators):
            runtime = 0 if denominator == 0 else _trunc(numerator, denominator)
            rtl = _rtl_restoring_divide(numerator, denominator)
            if runtime != rtl:
                mismatch = {
                    "numerator": numerator, "denominator": denominator,
                    "runtime": runtime, "rtl": rtl,
                }
                break
            passed += 1
        if mismatch is not None:
            break
    quotient_abs_bound = (numerator_abs_bound // denominator_min)
    invariant = {
        "magnitude_decomposition": "abs(numerator)=abs(quotient)*denominator+remainder",
        "remainder_interval": "0<=remainder<denominator",
        "sign_rule": "sign(quotient)=sign(numerator) for positive denominator",
        "full_interval_entailment": (
            "restoring long division emits the unique magnitude quotient by induction over all "
            "96 dividend bits; the recorded quotient bound makes low32 truncation lossless"
        ),
        "quotient_abs_bound": quotient_abs_bound,
        "quotient_fits_signed_int32": quotient_abs_bound < (1 << 31),
    }
    return {
        "status": ("representatives_and_invariant_proven"
                   if mismatch is None and invariant["quotient_fits_signed_int32"]
                   else "identity_frontier"),
        "certified_domain": {
            "numerator_min": -numerator_abs_bound,
            "numerator_max": numerator_abs_bound,
            "denominator_min": denominator_min,
            "denominator_max": denominator_max,
        },
        "zero_denominator_policy": "quotient_zero",
        "comparison": {"pass_count": passed, "mismatch_witness": mismatch},
        "algebraic_invariant": invariant,
    }


def _attention_proof(layer: int, manifest: Mapping[str, Any]) -> dict[str, Any]:
    bounds = {}
    for projection in ("q", "k", "v"):
        boundary = f"transformer.h.{layer}.attn.attention.{projection}_proj.output"
        lower, upper = _activation_bounds(boundary, manifest)
        bounds[projection] = (lower, upper)
    q_abs = [max(abs(lo), abs(hi)) for lo, hi in zip(*bounds["q"], strict=True)]
    k_abs = [max(abs(lo), abs(hi)) for lo, hi in zip(*bounds["k"], strict=True)]
    v_abs = [max(abs(lo), abs(hi)) for lo, hi in zip(*bounds["v"], strict=True)]
    score_intervals = []
    for head in range(16):
        lower, upper = 0, 0
        for index in range(head * 4, head * 4 + 4):
            qlo, qhi = bounds["q"][0][index], bounds["q"][1][index]
            klo, khi = bounds["k"][0][index], bounds["k"][1][index]
            products = (qlo * klo, qlo * khi, qhi * klo, qhi * khi)
            lower += min(products)
            upper += max(products)
        score_intervals.append((lower, upper))
    score_accum = max(max(abs(lo), abs(hi)) for lo, hi in score_intervals)
    score_code_lower = min(lo >> 24 for lo, _ in score_intervals)
    score_code_upper = max(hi >> 24 for _, hi in score_intervals)
    score_code = max(abs(score_code_lower), abs(score_code_upper))
    delta_abs = score_code_upper - score_code_lower
    numerator = 32 * (1 << 20) * max(v_abs)
    denominator_min, denominator_max = 1 << 20, 32 * (1 << 20)
    rounded_numerator = numerator + denominator_max // 2
    quotient = rounded_numerator // denominator_min
    inequalities = {
        "score_accumulator_fits_runtime_signed_int64": score_accum < (1 << 63),
        "score_accumulator_fits_rtl_signed_96": score_accum < (1 << 95),
        "arithmetic_shift_24_fits_signed_int32": score_code < (1 << 31),
        "score_max_sentinel_is_below_reachable_scores": score_code_lower > -(1 << 31) + 1,
        "delta_subtraction_fits_signed_int32": delta_abs < (1 << 31),
        "probability_values_fit_unsigned_21": (1 << 20) < (1 << 21),
        "probability_sum_fits_runtime_signed_int64": denominator_max < (1 << 63),
        "probability_sum_fits_rtl_unsigned_64": denominator_max < (1 << 64),
        "context_numerator_fits_runtime_signed_int64": rounded_numerator < (1 << 63),
        "context_numerator_fits_rtl_signed_96": rounded_numerator < (1 << 95),
        "rounding_correction_preserves_signed_96": rounded_numerator < (1 << 95),
        "divider_quotient_fits_signed_int32": quotient < (1 << 31),
    }
    return {
        "name": f"transformer.h.{layer}.attn.attention",
        "input_bounds_q16_16": {
            key: {"lower_by_channel": value[0], "upper_by_channel": value[1]}
            for key, value in bounds.items()
        },
        "proof": {
            "score_accumulator_abs_bound": score_accum,
            "score_accumulator_intervals_by_head": [list(item) for item in score_intervals],
            "score_code_interval": [score_code_lower, score_code_upper],
            "score_code_abs_bound": score_code,
            "delta_interval": [-delta_abs, 0],
            "probability_sum_interval": [denominator_min, denominator_max],
            "context_rounded_numerator_abs_bound": rounded_numerator,
            "divider_quotient_abs_bound": quotient,
            "sequence_length_max": 32,
            "inequalities": inequalities,
            "all": all(inequalities.values()),
        },
    }


def _weight_codes(name: str, manifest: Mapping[str, Any], weights: bytes) -> torch.Tensor:
    descriptor = manifest["tensors"][name]
    offset = int(descriptor["offset"])
    raw = weights[offset:offset + int(descriptor["nbytes"])]
    return torch.frombuffer(bytearray(raw), dtype=torch.int8).reshape(
        descriptor["logical_shape"]
    ).to(torch.int64)


def _gemv_specs() -> list[tuple[str, str, str | None, str | None]]:
    specs = []
    for layer in range(8):
        block, source = f"blocks.{layer}", f"transformer.h.{layer}"
        specs.extend((
            (f"{source}.attn.attention.q_proj", f"{block}.attn.q.weight", None,
             f"{source}.attn.attention.q_proj.output"),
            (f"{source}.attn.attention.k_proj", f"{block}.attn.k.weight", None,
             f"{source}.attn.attention.k_proj.output"),
            (f"{source}.attn.attention.v_proj", f"{block}.attn.v.weight", None,
             f"{source}.attn.attention.v_proj.output"),
            (f"{source}.attn.attention.out_proj", f"{block}.attn.out.weight",
             f"{block}.attn.out.bias", f"{source}.attn.attention.out_proj.output"),
            (f"{source}.mlp.c_fc", f"{block}.mlp.fc.weight", f"{block}.mlp.fc.bias",
             f"{source}.mlp.c_fc.output"),
            (f"{source}.mlp.c_proj", f"{block}.mlp.proj.weight", f"{block}.mlp.proj.bias",
             f"{source}.mlp.c_proj.output"),
        ))
    specs.append(("lm_head", "token_embedding.weight", None, None))
    return specs


def _first_unsafe_gemv_output(input_min: int, input_max: int, input_scales: list[int],
                              weight_codes: torch.Tensor) -> tuple[int, str] | None:
    signed64_min, signed64_max = -(1 << 63), (1 << 63) - 1
    for output, row in enumerate(weight_codes.tolist()):
        prefix_min = prefix_max = 0
        for scale, weight in zip(input_scales, row, strict=True):
            scaled_candidates = (input_min * scale, input_max * scale)
            if min(scaled_candidates) < signed64_min or max(scaled_candidates) > signed64_max:
                return output, "scaled_input_fits_signed_int64"
            candidates = (scaled_candidates[0] * weight, scaled_candidates[1] * weight)
            if min(candidates) < signed64_min or max(candidates) > signed64_max:
                return output, "gemv_term_fits_signed_int64"
            prefix_min += min(candidates)
            prefix_max += max(candidates)
            if prefix_min < signed64_min or prefix_max > signed64_max:
                return output, "serial_accumulator_prefix_fits_signed_int64"
    return None


def _gemv_failure(name: str, weight_name: str, bias_name: str | None,
                  output_boundary: str | None, output_count: int,
                  output: int, inequality: str) -> dict[str, Any]:
    return {
        "name": name, "weight": weight_name, "bias": bias_name,
        "input_boundary": f"{name}.input", "output_boundary": output_boundary,
        "output_count": output_count, "status": "identity_frontier",
        "first_failure_witness": {
            "module": name, "output": output, "term": inequality,
        },
    }


def _prove_gemv(name: str, weight_name: str, bias_name: str | None,
                output_boundary: str | None, manifest: Mapping[str, Any], weights: bytes,
                scales_image: bytes, input_code_min: int, input_code_max: int) -> dict[str, Any]:
    input_scales = torch.round(torch.tensor(
        manifest["activation_scales"][f"{name}.input"], dtype=torch.float64
    ) * (1 << Q_SCALE)).to(torch.int64)
    codes = _weight_codes(weight_name, manifest, weights)
    unsafe = _first_unsafe_gemv_output(
        input_code_min, input_code_max, input_scales.tolist(), codes
    )
    if unsafe is not None:
        output, inequality = unsafe
        return _gemv_failure(
            name, weight_name, bias_name, output_boundary,
            int(codes.shape[0]), output, inequality,
        )

    lower_scaled = input_scales * input_code_min
    upper_scaled = input_scales * input_code_max
    term_a, term_b = codes * lower_scaled.unsqueeze(0), codes * upper_scaled.unsqueeze(0)
    term_lower, term_upper = torch.minimum(term_a, term_b), torch.maximum(term_a, term_b)
    prefix_lower, prefix_upper = torch.cumsum(term_lower, dim=1), torch.cumsum(term_upper, dim=1)
    accumulator_min, accumulator_max = term_lower.sum(dim=1), term_upper.sum(dim=1)
    serial_abs = int(torch.maximum(prefix_lower.abs(), prefix_upper.abs()).max())
    term_abs = int(torch.maximum(term_lower.abs(), term_upper.abs()).max())
    scaled_input_abs = int(torch.maximum(lower_scaled.abs(), upper_scaled.abs()).max())
    weight_scales = _q24_scales(manifest["tensors"][weight_name], scales_image)
    accumulator_min_values = accumulator_min.tolist()
    accumulator_max_values = accumulator_max.tolist()
    weight_scale_values = weight_scales.tolist()
    bias = ([0] * len(accumulator_min_values) if bias_name is None
            else _parameter_q16(bias_name, manifest, weights))
    product_min: list[int] = []
    product_max: list[int] = []
    pre_min: list[int] = []
    pre_max: list[int] = []
    for output, (accumulator_lo, accumulator_hi, scale, offset) in enumerate(zip(
        accumulator_min_values, accumulator_max_values, weight_scale_values, bias, strict=True
    )):
        product_lo, product_hi = accumulator_lo * scale, accumulator_hi * scale
        if max(abs(product_lo), abs(product_hi)) >= (1 << 63):
            return _gemv_failure(
                name, weight_name, bias_name, output_boundary, int(codes.shape[0]),
                output, "weight_scale_product_fits_signed_int64",
            )
        pre_lo = _round_shift(product_lo, 32) + offset
        pre_hi = _round_shift(product_hi, 32) + offset
        if pre_lo < -(1 << 31) or pre_hi >= (1 << 31):
            return _gemv_failure(
                name, weight_name, bias_name, output_boundary, int(codes.shape[0]),
                output, "pre_output_q16_fits_signed_int32",
            )
        product_min.append(product_lo)
        product_max.append(product_hi)
        pre_min.append(pre_lo)
        pre_max.append(pre_hi)
    inequalities = {
        "scaled_input_fits_signed_int64": scaled_input_abs < (1 << 63),
        "gemv_term_fits_signed_int64": term_abs < (1 << 63),
        "serial_accumulator_prefix_fits_signed_int64": serial_abs < (1 << 63),
        "weight_scale_product_fits_signed_int64": max(
            max(abs(value) for value in product_min), max(abs(value) for value in product_max)
        ) < (1 << 63),
        "pre_output_q16_fits_signed_int32": (
            min(pre_min) >= -(1 << 31) and max(pre_max) < (1 << 31)
        ),
    }
    _require(all(inequalities.values()), "internal_gemv_proof_error", name)
    arrays = {
        "accumulator_min": accumulator_min_values,
        "accumulator_max": accumulator_max_values,
        "pre_output_q16_min": pre_min,
        "pre_output_q16_max": pre_max,
    }
    return {
        "name": name, "weight": weight_name, "bias": bias_name,
        "input_boundary": f"{name}.input", "output_boundary": output_boundary,
        "input_code_domain": [input_code_min, input_code_max],
        "input_scale_q8_24": {
            "count": len(input_scales), "values_sha256": canonical_sha256(input_scales.tolist()),
            "minimum": int(input_scales.min()), "maximum": int(input_scales.max()),
        },
        "weight_identity": {
            "codes_sha256": canonical_sha256(codes.tolist()),
            "scales_sha256": canonical_sha256(weight_scales.tolist()),
        },
        "output_count": len(pre_min),
        "status": "proven",
        "first_failure_witness": None,
        "per_output_summaries": {
            term: summarize_ordered_values(values, name, term)
            for term, values in arrays.items()
        },
        "proof": {
            "scaled_input_abs_bound": scaled_input_abs,
            "gemv_term_abs_bound": term_abs,
            "serial_accumulator_prefix_abs_bound": serial_abs,
            "weight_scale_product_abs_bound": max(
                max(abs(value) for value in product_min), max(abs(value) for value in product_max)
            ),
            "pre_output_q16_abs_bound": max(
                max(abs(value) for value in pre_min), max(abs(value) for value in pre_max)
            ),
            "inequalities": inequalities,
        },
    }


def derive_gemv_certificate(package: Path, *, input_code_min: int = -128,
                            input_code_max: int = 127) -> dict[str, Any]:
    package = Path(package)
    manifest, weights, scales_image = _read_package(package)
    calls = []
    failure = None
    for specification in _gemv_specs():
        call = _prove_gemv(
            *specification, manifest, weights, scales_image,
            input_code_min, input_code_max,
        )
        calls.append(call)
        if call["status"] != "proven":
            failure = call
            break
    return {
        "status": "all_preoutput_q16_ranges_proven" if failure is None else "identity_frontier",
        "domain_derivation": (
            "every GEMV input passes through signed-int8 activation Q/DQ; therefore all accepted "
            "token contexts are contained in the independent per-channel code box"
        ),
        "input_code_domain": [input_code_min, input_code_max],
        "calls": calls,
        "failing_call": failure["name"] if failure else None,
        "failing_output": failure["first_failure_witness"]["output"] if failure else None,
        "failing_inequality": failure["first_failure_witness"]["term"] if failure else None,
        "first_failure_witness": failure["first_failure_witness"] if failure else None,
    }


def prove_attention_operator_semantics(calls: list[dict[str, Any]],
                                       exp_lut: list[int]) -> dict[str, Any]:
    comparisons = {
        name: {"pass_count": 0, "mismatch_witness": None}
        for name in ("score_sum_shift", "score_max_delta", "probability_sum", "context_numerator")
    }
    for call in calls:
        q = call["input_bounds_q16_16"]["q"]
        k = call["input_bounds_q16_16"]["k"]
        v = call["input_bounds_q16_16"]["v"]
        for head in range(16):
            indices = range(head * 4, head * 4 + 4)
            patterns = (
                ([q["lower_by_channel"][i] for i in indices],
                 [k["lower_by_channel"][i] for i in indices]),
                ([q["upper_by_channel"][i] for i in indices],
                 [k["upper_by_channel"][i] for i in indices]),
                ([q["lower_by_channel"][i] if i % 2 else q["upper_by_channel"][i]
                  for i in indices],
                 [k["upper_by_channel"][i] if i % 2 else k["lower_by_channel"][i]
                  for i in indices]),
            )
            scores = []
            for q_values, k_values in patterns:
                runtime_total = sum(a * b for a, b in zip(q_values, k_values, strict=True))
                rtl_total = 0
                for a, b in zip(q_values, k_values, strict=True):
                    rtl_total = _signed(rtl_total + _signed(a, 32) * _signed(b, 32), 96)
                runtime, rtl = runtime_total >> 24, _signed(rtl_total, 96) >> 24
                comparison = comparisons["score_sum_shift"]
                if runtime != rtl and comparison["mismatch_witness"] is None:
                    comparison["mismatch_witness"] = {
                        "call": call["name"], "head": head, "runtime": runtime, "rtl": rtl,
                    }
                else:
                    comparison["pass_count"] += 1
                scores.append(runtime)
            runtime_max = max(scores)
            rtl_max = -(1 << 31) + 1
            for score in scores:
                if _signed(score, 32) > rtl_max:
                    rtl_max = _signed(score, 32)
            runtime_delta = [score - runtime_max for score in scores]
            rtl_delta = [_signed(score - rtl_max, 32) for score in scores]
            comparison = comparisons["score_max_delta"]
            if (runtime_max, runtime_delta) != (rtl_max, rtl_delta) \
                    and comparison["mismatch_witness"] is None:
                comparison["mismatch_witness"] = {"call": call["name"], "head": head}
            else:
                comparison["pass_count"] += 1

        probability_vectors = (
            [1 << 20],
            [exp_lut[0], 1 << 20],
            [exp_lut[2048]] * 31 + [1 << 20],
            [1 << 20] * 32,
        )
        for values in probability_vectors:
            runtime = sum(values)
            rtl = 0
            for value in values:
                rtl = (rtl + value) & ((1 << 64) - 1)
            comparison = comparisons["probability_sum"]
            if runtime != rtl and comparison["mismatch_witness"] is None:
                comparison["mismatch_witness"] = {"call": call["name"], "values": values}
            else:
                comparison["pass_count"] += 1

        for channel, (lower, upper) in enumerate(zip(
            v["lower_by_channel"], v["upper_by_channel"], strict=True
        )):
            for value, probability in ((lower, 1 << 20), (upper, 1 << 20),
                                       (lower, exp_lut[0]), (upper, exp_lut[2048])):
                runtime = sum(probability * value for _ in range(32))
                rtl = 0
                for _ in range(32):
                    rtl = _signed(rtl + probability * _signed(value, 32), 96)
                comparison = comparisons["context_numerator"]
                if runtime != rtl and comparison["mismatch_witness"] is None:
                    comparison["mismatch_witness"] = {
                        "call": call["name"], "channel": channel,
                        "runtime": runtime, "rtl": rtl,
                    }
                else:
                    comparison["pass_count"] += 1

    numerator_abs_bound = max(
        call["proof"]["context_rounded_numerator_abs_bound"] for call in calls
    )
    divider = prove_divider_semantics(numerator_abs_bound, 1 << 20, 32 * (1 << 20))
    equivalent = (all(item["mismatch_witness"] is None for item in comparisons.values())
                  and divider["status"] == "representatives_and_invariant_proven")
    return {
        "status": ("source_derived_equivalent_on_certified_intervals"
                   if equivalent else "identity_frontier"),
        "equations": {
            "score_sum": "sum_{dimension=0..3}(signed32(q)*signed32(k)); runtime signed64, RTL signed96",
            "score_shift": "arithmetic_shift_right(score_sum,24) into signed32",
            "score_max": "signed maximum over causal positions from RTL sentinel -2147483647",
            "delta": "signed32(score-score_max), then clamp to [-4096,0]",
            "probability_sum": "sum causal unsigned21 Q0.20 probabilities; runtime signed64, RTL unsigned64",
            "context_numerator": "sum(probability_unsigned21*signed32(value)); runtime signed64, RTL signed96",
            "rounding_correction": "numerator<0 ? numerator-floor(denominator/2) : numerator+floor(denominator/2)",
            "restoring_division": "truncate_toward_zero(corrected_signed96/positive_unsigned64), signed32 quotient",
        },
        "differential_checks": comparisons,
        "divider": divider,
    }


def derive_certificate(contract_path: Path, audit_path: Path, profile_path: Path,
                       softmax_path: Path, package: Path, reference_root: Path,
                       repo_root: Path) -> dict[str, Any]:
    contract_path, audit_path, profile_path = map(Path, (contract_path, audit_path, profile_path))
    softmax_path, package, reference_root, repo_root = map(
        Path, (softmax_path, package, reference_root, repo_root)
    )
    expected_files = {
        contract_path: CONTRACT_SHA256, audit_path: AUDIT_FILE_SHA256,
        profile_path: PROFILE_SHA256, softmax_path: SOFTMAX_SHA256,
        repo_root / LAYERNORM_RECEIPT: LAYERNORM_RECEIPT_SHA256,
        repo_root / GELU_RECEIPT: GELU_RECEIPT_SHA256,
        repo_root / GELU_ADAPTER: GELU_ADAPTER_SHA256,
    }
    for path, expected in expected_files.items():
        _require(path.is_file() and sha256_file(path) == expected,
                 "authority_identity_mismatch", str(path))
    contract, audit = _load(contract_path), _load(audit_path)
    _require(audit.get("status") == "authenticated", "identity_frontier", "audit")
    _require(contract["deployed_profile"]["revision"] == REFERENCE_REVISION,
             "source_revision_mismatch", REFERENCE_REVISION)
    for name, identity in contract["package"]["files"].items():
        path = package / name
        _require(path.is_file() and path.stat().st_size == identity["size"]
                 and sha256_file(path) == identity["sha256"],
                 "package_identity_mismatch", name)
    materialized_sources = materialize_semantic_sources(
        contract_path, audit_path, reference_root
    )
    source_identity = {
        name: {"git_blob_sha1": value["git_blob_sha1"], "sha256": value["sha256"]}
        for name, value in materialized_sources.items()
    }
    runtime_gelu_lut, rtl_gelu_lut, exp_luts = materialize_luts(materialized_sources)
    runtime_exp_lut, rtl_exp_lut = exp_luts

    manifest, weights, scales_image = _read_package(package)
    token_lower, token_upper = _embedding_bounds(
        "token_embedding.weight", 50257, manifest, weights, scales_image
    )
    position_lower, position_upper = _embedding_bounds(
        "position_embedding.weight", 32, manifest, weights, scales_image
    )
    lower = [a + b for a, b in zip(token_lower, position_lower, strict=True)]
    upper = [a + b for a, b in zip(token_upper, position_upper, strict=True)]
    calls = []
    for layer in range(8):
        block = f"blocks.{layer}"
        ln1 = prove_layernorm_interval(
            f"transformer.h.{layer}.ln_1", lower, upper,
            _parameter_q16(f"{block}.ln1.weight", manifest, weights),
            _parameter_q16(f"{block}.ln1.bias", manifest, weights),
        )
        ln1["topology"] = "embedding_plus_prior_attention_and_mlp_qdq_residuals"
        calls.append(ln1)
        add_lower, add_upper = _activation_bounds(
            f"transformer.h.{layer}.attn.attention.out_proj.output", manifest
        )
        lower = [a + b for a, b in zip(lower, add_lower, strict=True)]
        upper = [a + b for a, b in zip(upper, add_upper, strict=True)]
        ln2 = prove_layernorm_interval(
            f"transformer.h.{layer}.ln_2", lower, upper,
            _parameter_q16(f"{block}.ln2.weight", manifest, weights),
            _parameter_q16(f"{block}.ln2.bias", manifest, weights),
        )
        ln2["topology"] = "layer_input_plus_attention_out_qdq"
        calls.append(ln2)
        add_lower, add_upper = _activation_bounds(
            f"transformer.h.{layer}.mlp.c_proj.output", manifest
        )
        lower = [a + b for a, b in zip(lower, add_lower, strict=True)]
        upper = [a + b for a, b in zip(upper, add_upper, strict=True)]
    final = prove_layernorm_interval(
        "transformer.ln_f", lower, upper,
        _parameter_q16("final_ln.weight", manifest, weights),
        _parameter_q16("final_ln.bias", manifest, weights),
    )
    final["topology"] = "embedding_plus_all_eight_attention_and_mlp_qdq_residuals"
    calls.append(final)
    gelu_calls = [_gelu_proof(layer, manifest) for layer in range(8)]
    attention_calls = [_attention_proof(layer, manifest) for layer in range(8)]
    gelu_semantics = prove_gelu_semantics(runtime_gelu_lut, rtl_gelu_lut)
    exp_semantics = prove_exp_semantics(runtime_exp_lut, rtl_exp_lut)
    attention_operators = prove_attention_operator_semantics(attention_calls, rtl_exp_lut)
    gemv = derive_gemv_certificate(package)
    failure = next((call for call in calls if call["status"] != "proven"), None)
    nonlinear_failure = next(
        (call for call in gelu_calls + attention_calls if not call["proof"]["all"]), None
    )
    semantic_failure = next((name for name, proof in (
        ("gelu", gelu_semantics), ("attention_exp", exp_semantics),
        ("attention_operators", attention_operators),
    ) if proof["status"] == "identity_frontier"), None)
    status = (
        "proven_reachable_domain_equivalent"
        if failure is None and nonlinear_failure is None and semantic_failure is None
        and gemv["status"] == "all_preoutput_q16_ranges_proven"
        else "identity_frontier"
    )
    certificate: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "identity": {
            "contract_sha256": sha256_file(contract_path),
            "audit_file_sha256": sha256_file(audit_path),
            "audit_payload_sha256": audit["sha256"],
            "profile_sha256": sha256_file(profile_path),
            "softmax_authority_sha256": sha256_file(softmax_path),
            "layernorm_conflict_receipt_sha256": sha256_file(repo_root / LAYERNORM_RECEIPT),
            "gelu_equivalence_receipt_sha256": sha256_file(repo_root / GELU_RECEIPT),
            "gelu_adapter_sha256": sha256_file(repo_root / GELU_ADAPTER),
            "package_manifest_sha256": sha256_file(package / "manifest.json"),
            "package_weights_sha256": sha256_file(package / "weights.bin"),
            "package_scales_sha256": sha256_file(package / "scales.bin"),
            "reference_revision": REFERENCE_REVISION,
            "semantic_sources": source_identity,
            "certifier_sha256": sha256_file(Path(__file__)),
        },
        "source_authentication": {
            "status": "materialized_git_blobs_match_authenticated_closure",
            "revision": REFERENCE_REVISION,
            "source_count": len(source_identity),
            "materialized_sources": source_identity,
            "closure_sha256": canonical_sha256(source_identity),
        },
        "materialization": {
            "parameters": "nearest_ties_to_even_float32_to_signed_q16.16",
            "scales": "nearest_ties_to_even_float32_to_unsigned_q8.24",
            "all_package_parameters_fit_signed_int32": True,
        },
        "layer_norm": {
            "domain_derivation": "per_channel_embedding_extrema_plus_qdq_clamped_projection_output_extrema_through_residual_topology",
            "calls": calls,
            "conclusion": (
                "runtime_and_synthesizable_rtl_identical_on_reachable_domain"
                if failure is None else "identity_frontier"
            ),
            "failing_call": failure["name"] if failure else None,
            "failing_inequality": failure["failing_inequality"] if failure else None,
        },
        "gemv": gemv,
        "nonlinear": {
            "gelu": {
                "runtime_source": "tinystories/hardware_reference.py:fixed_gelu",
                "rtl_sources": ["tinystories/rtl_memories.py", "fpga/rtl/gptneo_gelu.sv",
                                str(GELU_ADAPTER)],
                "semantic_equivalence": gelu_semantics,
                "calls": gelu_calls,
            },
            "attention_softmax": {
                "runtime_source": "tinystories/hardware_reference.py:FixedGPTNeo._attention",
                "rtl_sources": ["fpga/rtl/gptneo_attention.sv", "fpga/rtl/gptneo_iterative_divider.sv"],
                "authenticated_checkpoint_artifact_sha256": sha256_file(softmax_path),
                "exp_equivalence": exp_semantics,
                "operator_equivalence": attention_operators,
                "calls": attention_calls,
            },
            "failing_call": (nonlinear_failure["name"] if nonlinear_failure else semantic_failure),
        },
    }
    certificate["certificate_sha256"] = certificate_sha256(certificate)
    return certificate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--softmax", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = derive_certificate(
        args.contract, args.audit, args.profile, args.softmax, args.package,
        args.reference_root, args.repo_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(value["status"])


if __name__ == "__main__":
    main()
