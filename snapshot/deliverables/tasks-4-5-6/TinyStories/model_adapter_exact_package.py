from __future__ import annotations

"""Authenticated integer/fixed-point TinyStories-1M package adapter.

The public reference-package adapter remains an FP32 reconstruction.  This
module reuses only its package tensor mapping and executes the selected
``fixed_hardware_reference`` semantics explicitly with tensor-visible integer
codes, Q8.24 scales, signed Q16.16 values, signed-64 serial accumulation,
rounding, clamp, and LUT operations.
"""

import hashlib
import importlib.util
import json
import math
import struct
from pathlib import Path
from typing import Any, Callable, Mapping

import torch

from TinyStories.serial_gemv_boundary import serial_gemv


def _load_reference_adapter() -> Any:
    path = Path(__file__).with_name("model_adapter_reference_package.py")
    spec = importlib.util.spec_from_file_location("tinystories_reference_package_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load package mapping adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference_adapter = _load_reference_adapter()


Q_VALUE = 16
Q_SCALE = 24
MAX_CONTEXT = 32
CHECKPOINT_SHAPES: dict[str, tuple[int, ...]] = {
    "block.input": (64,),
    "block.ln_1.output": (64,),
    "block.attention.q": (16, 4),
    "block.attention.k": (16, 4),
    "block.attention.v": (16, 4),
    "block.attention.output": (64,),
    "block.residual.attention": (64,),
    "block.ln_2.output": (64,),
    "block.mlp.fc_in": (256,),
    "block.mlp.activation": (256,),
    "block.mlp.fc_out": (64,),
    "block.output": (64,),
}
AUDIT_NAME = "tinystories-1m-exact-input-audit.json"
FIXED_PROFILE_RELATIVE = Path("artifacts/reference/tinystories-1m-fixed-hardware-qdq-profile.json")
_PROVENANCE_RELATIVE = Path("artifacts/reference/tinystories-1m-export-provenance.json")


def _load_export_provenance() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    with (root / _PROVENANCE_RELATIVE).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or not isinstance(value.get("expected"), dict):
        raise RuntimeError("invalid TinyStories export provenance manifest")
    return value["expected"]


_PROVENANCE = _load_export_provenance()
FINITE_VALIDATOR_RELATIVE = Path("scripts/comparison/audit_tinystories_1m_exact_input.py")
FINITE_VALIDATOR_SHA256 = "93b51d87dbb0a61911abdb951573c37018c90358dfed2f27b67832a6c1eea92d"
FROZEN_FIXED_PROFILE_SHA256 = _PROVENANCE["fixed_profile"]
FROZEN_PROFILE_SELF_SHA256 = _PROVENANCE["profile_self"]
FROZEN_CONTRACT_SHA256 = _PROVENANCE["contract"]
FROZEN_AUDIT_FILE_SHA256 = _PROVENANCE["audit_file"]
FROZEN_PACKAGE_RECEIPT_SHA256 = _PROVENANCE["package_receipt"]
REACHABLE_CERTIFICATE_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-reachable-domain.json")
REACHABLE_CERTIFICATE_SHA256 = _PROVENANCE["reachable_certificate"]
FIXED_LOGITS_ORACLE_RELATIVE = Path("artifacts/reference/tinystories-1m-fixed-logits-oracle.json")
FIXED_LOGITS_ORACLE_SHA256 = _PROVENANCE["fixed_logits_oracle"]
FROZEN_GENERATION_ARTIFACT_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-generation.json")
FROZEN_GENERATION_VERIFIER_RELATIVE = Path("scripts/comparison/verify_tinystories_1m_exact_generation.py")

QDQ_BOUNDARY_NAMES: tuple[str, ...] = tuple(
    name
    for layer in range(8)
    for module in (
        f"transformer.h.{layer}.attn.attention.q_proj",
        f"transformer.h.{layer}.attn.attention.k_proj",
        f"transformer.h.{layer}.attn.attention.v_proj",
        f"transformer.h.{layer}.attn.attention.out_proj",
        f"transformer.h.{layer}.mlp.c_fc",
        f"transformer.h.{layer}.mlp.c_proj",
    )
    for name in (f"{module}.input", f"{module}.output")
) + ("lm_head.input",)
GEMV_NAMES: tuple[str, ...] = tuple(
    module
    for layer in range(8)
    for module in (
        f"transformer.h.{layer}.attn.attention.q_proj",
        f"transformer.h.{layer}.attn.attention.k_proj",
        f"transformer.h.{layer}.attn.attention.v_proj",
        f"transformer.h.{layer}.attn.attention.out_proj",
        f"transformer.h.{layer}.mlp.c_fc",
        f"transformer.h.{layer}.mlp.c_proj",
    )
) + ("lm_head",)
NONLINEAR_BOUNDARY_NAMES: tuple[str, ...] = tuple(
    name
    for layer in range(8)
    for name in (
        f"transformer.h.{layer}.ln_1.output",
        f"transformer.h.{layer}.attn.attention.context",
        f"transformer.h.{layer}.ln_2.output",
        f"transformer.h.{layer}.mlp.gelu.output",
    )
) + ("transformer.ln_f.output",)


class ExactModelError(ValueError):
    """The exact model cannot be constructed or entered without approximation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ExactModelError(code, message)


def _validate_frozen_generation_artifact(artifact_path: Path) -> None:
    """Validate the exact immutable predecessor artifact in its historical location."""

    expected_artifact = _repo_root() / FROZEN_GENERATION_ARTIFACT_RELATIVE
    try:
        supplied_artifact = Path(artifact_path)
    except TypeError as error:
        raise ExactModelError(
            "successor_predecessor_unverified", "invalid frozen-generation artifact path"
        ) from error
    _require(supplied_artifact.resolve() == expected_artifact.resolve(),
             "successor_predecessor_unverified", "unexpected frozen-generation artifact path")
    verifier_path = _repo_root() / FROZEN_GENERATION_VERIFIER_RELATIVE
    spec = importlib.util.spec_from_file_location("tinystories_frozen_generation_verifier", verifier_path)
    _require(spec is not None and spec.loader is not None,
             "successor_predecessor_unverified", "frozen generation verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        artifact = json.loads(expected_artifact.read_text(encoding="utf-8"))
        module.validate_artifact(artifact, _repo_root())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ExactModelError("successor_predecessor_unverified", str(error)) from error


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExactModelError("invalid_json", f"{label}: {error}") from error
    _require(isinstance(value, dict), "invalid_json", f"{label} must be an object")
    return value


def round_shift_signed(values: torch.Tensor, shift: int) -> torch.Tensor:
    """Signed nearest rounding, with ties away from zero, then right shift."""

    _require(isinstance(shift, int) and 0 <= shift < 63, "invalid_shift", str(shift))
    values = values.to(torch.int64)
    if shift == 0:
        return values.clone()
    divisor = 1 << shift
    floor_quotient = torch.div(values, divisor, rounding_mode="floor")
    floor_remainder = torch.remainder(values, divisor)
    negative = values < 0
    negative_quotient = -floor_quotient - (floor_remainder != 0).to(torch.int64)
    negative_remainder = torch.where(
        floor_remainder == 0, torch.zeros_like(floor_remainder), divisor - floor_remainder
    )
    quotient = torch.where(negative, negative_quotient, floor_quotient)
    remainder = torch.where(negative, negative_remainder, floor_remainder)
    rounded = quotient + (remainder >= (divisor >> 1)).to(torch.int64)
    return torch.where(values < 0, -rounded, rounded)


def activation_qdq(values_q16: torch.Tensor, scales_q24: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Execute one symmetric per-channel INT8 Q/DQ boundary explicitly."""

    values_q16 = values_q16.to(torch.int64)
    scales_q24 = scales_q24.to(torch.int64)
    if not torch.compiler.is_compiling():
        _require(bool(((values_q16 >= -(1 << 31)) & (values_q16 < (1 << 31))).all()),
                 "q16_range_violation", "activation input is outside signed Q16.16 int32")
        _require(bool(((scales_q24 > 0) & (scales_q24 < (1 << 24))).all()),
                 "q24_range_violation", "activation scale is outside unsigned Q8.24 u24")
    numerator = torch.bitwise_left_shift(values_q16, Q_SCALE - Q_VALUE)
    magnitude = torch.div(
        torch.abs(numerator) + torch.div(scales_q24, 2, rounding_mode="floor"),
        scales_q24,
        rounding_mode="floor",
    )
    signed = torch.where(numerator < 0, -magnitude, magnitude)
    codes = torch.clamp(signed, -128, 127).to(torch.int64)
    dequantized = round_shift_signed(codes * scales_q24, Q_SCALE - Q_VALUE)
    return codes, dequantized


def serial_gemv_accumulate(scaled_input_q24: torch.Tensor, weight_codes: torch.Tensor) -> torch.Tensor:
    """Ascending-index signed-64 serial MAC; torch int64 supplies two's-complement wrap."""

    scaled_input_q24 = scaled_input_q24.to(torch.int64)
    weight_codes = weight_codes.to(torch.int64)
    _require(scaled_input_q24.ndim == 2 and weight_codes.ndim == 2,
             "gemv_shape_mismatch", "serial GEMV expects two matrices")
    _require(scaled_input_q24.shape[1] == weight_codes.shape[1],
             "gemv_shape_mismatch", "input widths differ")
    accumulator = torch.zeros(
        (scaled_input_q24.shape[0], weight_codes.shape[0]),
        dtype=torch.int64,
        device=scaled_input_q24.device,
    )
    for input_index in range(weight_codes.shape[1]):
        term = scaled_input_q24[:, input_index:input_index + 1] * weight_codes[:, input_index]
        accumulator = accumulator + term
    return accumulator


def _trunc_divide(numerator: torch.Tensor, denominator: torch.Tensor) -> torch.Tensor:
    quotient = torch.div(torch.abs(numerator), torch.abs(denominator), rounding_mode="floor")
    return torch.where((numerator < 0) != (denominator < 0), -quotient, quotient)


def _integer_sqrt(values: torch.Tensor) -> torch.Tensor:
    """Vectorized restoring square root for non-negative signed-64 values."""

    remainder = values.to(torch.int64)
    result = torch.zeros_like(remainder)
    bit = 1 << 62
    for _ in range(32):
        candidate = result + bit
        take = remainder >= candidate
        remainder = torch.where(take, remainder - candidate, remainder)
        result = torch.where(take, torch.bitwise_right_shift(result, 1) + bit,
                             torch.bitwise_right_shift(result, 1))
        bit >>= 2
    return result


def _fixed_layer_norm(values: torch.Tensor, gamma: torch.Tensor, beta: torch.Tensor) -> torch.Tensor:
    original_shape = values.shape
    rows = values.reshape(-1, original_shape[-1]).to(torch.int64)
    width = rows.shape[1]
    row_sum = torch.sum(rows, dim=1, keepdim=True, dtype=torch.int64)
    mean_magnitude = torch.div(torch.abs(row_sum), width, rounding_mode="floor")
    mean = torch.where(row_sum < 0, -mean_magnitude, mean_magnitude)
    deltas = rows - mean
    variance = torch.div(torch.sum(deltas * deltas, dim=1, keepdim=True, dtype=torch.int64),
                         width, rounding_mode="floor") + 42950
    deviation = _integer_sqrt(variance)
    normalized = _trunc_divide(torch.bitwise_left_shift(deltas, Q_VALUE), deviation)
    affine = torch.bitwise_right_shift(normalized * gamma, Q_VALUE) + beta
    return affine.reshape(original_shape)


def _gelu_table() -> torch.Tensor:
    values = []
    for index in range(8192):
        value = -8.0 + index / 512.0
        gelu = 0.5 * value * (
            1.0 + math.tanh(math.sqrt(2.0 / math.pi) * (value + 0.044715 * value ** 3))
        )
        values.append(max(-32768, min(32767, round(gelu * 4096.0))))
    return torch.tensor(values, dtype=torch.int64)


def _exp_table() -> torch.Tensor:
    return torch.tensor(
        [round(math.exp((index - 4096) / 256.0) * (1 << 20)) for index in range(4096)],
        dtype=torch.int64,
    )


def _load_finite_validator() -> Callable[[Any], None]:
    path = _repo_root() / FINITE_VALIDATOR_RELATIVE
    _require(path.is_file() and _sha256(path) == FINITE_VALIDATOR_SHA256,
             "validator_identity_mismatch", str(path))
    spec = importlib.util.spec_from_file_location("tinystories_exact_finite_validator", path)
    _require(spec is not None and spec.loader is not None, "validator_identity_mismatch", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    validator = getattr(module, "validate_finite_adapter_input", None)
    _require(callable(validator), "validator_identity_mismatch", "finite validator missing")
    return validator


def _validate_arithmetic_identity(contract: Mapping[str, Any], audit: Mapping[str, Any],
                                  profile: Mapping[str, Any]) -> None:
    required_fixed = {
        "value_format": "signed Q16.16",
        "scale_format": "unsigned Q8.24",
        "gemv_accumulator": "signed 64-bit serial accumulator",
    }
    required_quantization = {
        "weights": "symmetric per-output INT8",
        "activations": "symmetric per-channel INT8",
        "activation_boundary_count": 97,
        "activation_vector_widths": [64, 256],
        "scale_format": "little-endian float32",
    }
    _require(contract.get("fixed_point") == required_fixed and audit.get("fixed_point") == required_fixed,
             "arithmetic_identity_mismatch", "fixed-point contract differs")
    _require(contract.get("quantization") == required_quantization
             and audit.get("quantization") == required_quantization,
             "arithmetic_identity_mismatch", "quantization contract differs")
    semantics = profile.get("semantics")
    _require(isinstance(semantics, dict)
             and semantics.get("execution_domain") == "integers_only_after_materialization",
             "arithmetic_identity_mismatch", "fixed profile execution domain differs")
    accumulation = semantics.get("accumulation", {}).get("synthesizable_rtl", {})
    _require(accumulation == {
        "operation": "serial_multiply_accumulate",
        "input_order": "ascending_input_index",
        "logical_width_bits": 64,
        "overflow": "twos_complement_wrap",
    }, "arithmetic_identity_mismatch", "serial accumulator semantics differ")
    activation = semantics.get("activation_conversion")
    _require(isinstance(activation, dict)
             and activation.get("rounding") == "nearest_ties_away_from_zero"
             and activation.get("clamp") == [-128, 127]
             and activation.get("zero_point") == 0,
             "arithmetic_identity_mismatch", "activation Q/DQ semantics differ")


def _validate_reachable_certificate(certificate: Mapping[str, Any], contract: Mapping[str, Any],
                                    audit: Mapping[str, Any]) -> None:
    _require(certificate.get("certificate_sha256") == canonical_sha256(
        {key: value for key, value in certificate.items() if key != "certificate_sha256"}
    ), "identity_frontier", "reachable-domain certificate self-hash differs")
    _require(certificate.get("schema") == "tinystories-1m-exact-reachable-domain-v1"
             and certificate.get("status") == "proven_reachable_domain_equivalent",
             "identity_frontier", "reachable-domain equivalence was not proven")
    identity = certificate.get("identity", {})
    _require(identity.get("contract_sha256") == FROZEN_CONTRACT_SHA256
             and identity.get("audit_file_sha256") == FROZEN_AUDIT_FILE_SHA256
             and identity.get("audit_payload_sha256") == audit.get("sha256")
             and identity.get("profile_sha256") == FROZEN_FIXED_PROFILE_SHA256
             and identity.get("package_manifest_sha256") == contract["package"]["manifest_sha256"]
             and identity.get("package_weights_sha256") == contract["package"]["sha256"]
             and identity.get("package_scales_sha256") == contract["package"]["files"]["scales.bin"]["sha256"],
             "identity_frontier", "reachable-domain certificate identity differs")
    expected_source_names = {
        "tinystories/hardware_reference.py", "tinystories/rtl_memories.py",
        "fpga/rtl/gptneo_layernorm.sv", "fpga/rtl/gptneo_gelu.sv",
        "fpga/rtl/gptneo_attention.sv", "fpga/rtl/gptneo_iterative_divider.sv",
    }
    expected_sources = {
        name: contract["deployed_profile"]["sources"][name] for name in expected_source_names
    }
    _require(set(identity.get("semantic_sources", {})) == expected_source_names
             and identity.get("semantic_sources") == expected_sources,
             "identity_frontier", "reachable-domain semantic source identity differs")
    source_authentication = certificate.get("source_authentication", {})
    _require(source_authentication.get("status")
             == "materialized_git_blobs_match_authenticated_closure"
             and source_authentication.get("revision") == contract["deployed_profile"]["revision"]
             and source_authentication.get("source_count") == len(expected_sources)
             and source_authentication.get("materialized_sources") == expected_sources
             and source_authentication.get("closure_sha256") == canonical_sha256(expected_sources),
             "identity_frontier", "semantic sources were not authenticated from pinned Git blobs")
    certifier = _repo_root() / "scripts/comparison/certify_tinystories_1m_exact_reachable_domain.py"
    _require(certifier.is_file() and identity.get("certifier_sha256") == _sha256(certifier),
             "identity_frontier", "reachable-domain certifier identity differs")
    layer_norm = certificate.get("layer_norm", {})
    calls = layer_norm.get("calls")
    expected_layernorm = tuple(
        name for layer in range(8)
        for name in (f"transformer.h.{layer}.ln_1", f"transformer.h.{layer}.ln_2")
    ) + ("transformer.ln_f",)
    _require(isinstance(calls, list) and tuple(call.get("name") for call in calls) == expected_layernorm
             and all(call.get("status") == "proven"
                     and all(call.get("proof", {}).get("inequalities", {}).values())
                     for call in calls)
             and layer_norm.get("conclusion")
             == "runtime_and_synthesizable_rtl_identical_on_reachable_domain",
             "identity_frontier", "LayerNorm reachable-domain proof is incomplete")
    nonlinear = certificate.get("nonlinear", {})
    gelu = nonlinear.get("gelu", {})
    attention = nonlinear.get("attention_softmax", {})
    gelu_calls = gelu.get("calls", [])
    attention_calls = attention.get("calls", [])
    _require(tuple(call.get("name") for call in gelu_calls)
             == tuple(f"transformer.h.{layer}.mlp.gelu" for layer in range(8))
             and tuple(call.get("name") for call in attention_calls)
             == tuple(f"transformer.h.{layer}.attn.attention" for layer in range(8))
             and all(call.get("proof", {}).get("all") for call in gelu_calls + attention_calls),
             "identity_frontier", "nonlinear reachable-domain proof is incomplete")
    gelu_semantics = gelu.get("semantic_equivalence", {})
    gelu_comparison = gelu_semantics.get("comparison", {})
    _require(gelu_semantics.get("status") == "exhaustive_runtime_rtl_equivalent"
             and gelu_semantics.get("input_domain") == {
                 "format": "signed_q4.12_int16", "minimum": -32768,
                 "maximum": 32767, "count": 65536,
             }
             and gelu_comparison.get("pass_count") == 65536
             and gelu_comparison.get("mismatch_witness") is None
             and gelu_semantics.get("runtime_lut", {}).get("values_sha256")
             == gelu_semantics.get("rtl_lut", {}).get("values_sha256"),
             "identity_frontier", "GELU exhaustive semantic proof is incomplete")
    exp_semantics = attention.get("exp_equivalence", {})
    exp_comparison = exp_semantics.get("comparison", {})
    _require(exp_semantics.get("status") == "exhaustive_effective_domain_equivalent"
             and exp_semantics.get("effective_delta_domain") == [-4096, 0]
             and exp_comparison.get("pass_count") == 4101
             and exp_comparison.get("outside_clamp_representatives")
             == [-2147483648, -4097, 1, 2147483647]
             and exp_comparison.get("mismatch_witness") is None
             and exp_semantics.get("runtime_lut", {}).get("values_sha256")
             == exp_semantics.get("rtl_lut", {}).get("values_sha256"),
             "identity_frontier", "attention exp semantic proof is incomplete")
    operators = attention.get("operator_equivalence", {})
    differential = operators.get("differential_checks", {})
    divider = operators.get("divider", {})
    _require(operators.get("status") == "source_derived_equivalent_on_certified_intervals"
             and set(operators.get("equations", {})) == {
                 "score_sum", "score_shift", "score_max", "delta", "probability_sum",
                 "context_numerator", "rounding_correction", "restoring_division",
             }
             and set(differential) == {
                 "score_sum_shift", "score_max_delta", "probability_sum", "context_numerator",
             }
             and all(item.get("pass_count", 0) > 0
                     and item.get("mismatch_witness") is None for item in differential.values())
             and divider.get("status") == "representatives_and_invariant_proven"
             and divider.get("zero_denominator_policy") == "quotient_zero"
             and divider.get("comparison", {}).get("pass_count", 0) >= 100
             and divider.get("comparison", {}).get("mismatch_witness") is None
             and divider.get("algebraic_invariant", {}).get("quotient_fits_signed_int32") is True,
             "identity_frontier", "attention operator/divider proof is incomplete")
    gemv = certificate.get("gemv", {})
    gemv_calls = gemv.get("calls", [])
    _require(gemv.get("status") == "all_preoutput_q16_ranges_proven"
             and tuple(call.get("name") for call in gemv_calls) == GEMV_NAMES
             and gemv.get("failing_call") is None
             and gemv.get("failing_output") is None
             and gemv.get("failing_inequality") is None
             and gemv.get("first_failure_witness") is None,
             "identity_frontier", "GEMV reachable-domain call coverage is incomplete")
    for call in gemv_calls:
        output_count = call.get("output_count")
        summaries = call.get("per_output_summaries", {})
        summary_terms = {
            "accumulator_min", "accumulator_max",
            "pre_output_q16_min", "pre_output_q16_max",
        }
        _require(call.get("status") == "proven"
                 and call.get("first_failure_witness") is None
                 and isinstance(output_count, int) and output_count > 0
                 and "per_output_bounds" not in call
                 and set(summaries) == summary_terms
                 and all(call.get("proof", {}).get("inequalities", {}).values())
                 and call.get("proof", {}).get("pre_output_q16_abs_bound", 1 << 31) < (1 << 31),
                 "identity_frontier", f"GEMV proof is incomplete: {call.get('name')}")
        for term, summary in summaries.items():
            digest = summary.get("ordered_values_sha256")
            minimum = summary.get("signed_minimum", {})
            maximum = summary.get("signed_maximum", {})
            absolute = summary.get("absolute_maximum", {})
            witness = summary.get("worst_case_witness", {})
            indices = (
                minimum.get("output_index"), maximum.get("output_index"),
                absolute.get("output_index"), witness.get("output"),
            )
            _require(isinstance(digest, str) and len(digest) == 64
                     and all(character in "0123456789abcdef" for character in digest)
                     and summary.get("element_count") == output_count
                     and all(isinstance(index, int) and 0 <= index < output_count
                             for index in indices)
                     and isinstance(minimum.get("value"), int)
                     and isinstance(maximum.get("value"), int)
                     and minimum["value"] <= maximum["value"]
                     and isinstance(absolute.get("value"), int)
                     and absolute.get("magnitude") == abs(absolute["value"])
                     and absolute["magnitude"] >= max(
                         abs(minimum["value"]), abs(maximum["value"])
                     )
                     and witness == {
                         "module": call["name"], "output": absolute["output_index"],
                         "term": term, "value": absolute["value"],
                         "absolute_value": absolute["magnitude"],
                     }
                     and summary.get("first_failure_witness") is None,
                     "identity_frontier", f"GEMV compact summary is incomplete: {call.get('name')}:{term}")
        pre_output_abs = max(
            summaries["pre_output_q16_min"]["absolute_maximum"]["magnitude"],
            summaries["pre_output_q16_max"]["absolute_maximum"]["magnitude"],
        )
        _require(pre_output_abs == call["proof"]["pre_output_q16_abs_bound"],
                 "identity_frontier", f"GEMV compact extrema differ: {call.get('name')}")


def _validate_fixed_logits_oracle(oracle: Mapping[str, Any], contract: Mapping[str, Any],
                                  audit: Mapping[str, Any]) -> torch.Tensor:
    _require(oracle.get("oracle_sha256") == canonical_sha256(
        {key: value for key, value in oracle.items() if key != "oracle_sha256"}
    ), "independent_logits_identity_mismatch", "oracle self-hash differs")
    _require(oracle.get("schema") == "tinystories-1m-fixed-logits-oracle-v1"
             and oracle.get("status") == "independent_pinned_fixed_reference"
             and oracle.get("prompt_tokens") == contract["reference"]["prompt_tokens"]
             and oracle.get("next_token") == contract["reference"]["tokens"][0],
             "independent_logits_identity_mismatch", "oracle fixture differs")
    identity = oracle.get("identity", {})
    _require(identity.get("contract_sha256") == FROZEN_CONTRACT_SHA256
             and identity.get("audit_file_sha256") == FROZEN_AUDIT_FILE_SHA256
             and identity.get("audit_payload_sha256") == audit.get("sha256")
             and identity.get("package_manifest_sha256") == contract["package"]["manifest_sha256"]
             and identity.get("package_weights_sha256") == contract["package"]["sha256"]
             and identity.get("package_scales_sha256") == contract["package"]["files"]["scales.bin"]["sha256"]
             and identity.get("reference_revision") == contract["deployed_profile"]["revision"],
             "independent_logits_identity_mismatch", "oracle provenance differs")
    expected_sources = {
        name: contract["deployed_profile"]["sources"][name]
        for name in identity.get("pinned_sources", {})
    }
    capture = _repo_root() / "scripts/comparison/capture_tinystories_1m_fixed_logits.py"
    _require(identity.get("pinned_sources") == expected_sources
             and capture.is_file() and identity.get("capture_script_sha256") == _sha256(capture),
             "independent_logits_identity_mismatch", "oracle source closure differs")
    logits = oracle.get("logits", {})
    values = logits.get("values")
    _require(logits.get("shape") == [50257] and logits.get("dtype") == "signed_q16.16_int64"
             and isinstance(values, list) and len(values) == 50257
             and all(isinstance(value, int) and not isinstance(value, bool) for value in values),
             "independent_logits_identity_mismatch", "oracle vector is malformed")
    packed = b"".join(struct.pack("<q", value) for value in values)
    _require(logits.get("canonical_sha256") == canonical_sha256(values)
             and logits.get("little_endian_int64_sha256") == hashlib.sha256(packed).hexdigest(),
             "independent_logits_identity_mismatch", "oracle vector hash differs")
    return torch.tensor(values, dtype=torch.int64)


def _authenticate_inputs(contract_path: Path, package_path: Path, *,
                         historical_generation_artifact_path: Path | None = None) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], torch.Tensor,
    dict[str, str],
]:
    if historical_generation_artifact_path is not None:
        _validate_frozen_generation_artifact(historical_generation_artifact_path)
    contract = _load_json(contract_path, "exact-input contract")
    audit_path = contract_path.with_name(AUDIT_NAME)
    audit = _load_json(audit_path, "exact-input audit")
    _require(audit.get("schema") == "tinystories-1m-exact-input-audit-v1",
             "identity_frontier", "audit schema differs")
    _require(audit.get("sha256") == canonical_sha256({k: v for k, v in audit.items() if k != "sha256"}),
             "identity_frontier", "audit self-hash differs")
    _require(audit.get("status") == "authenticated" and audit.get("conflicts") == [],
             "identity_frontier", "canonical audit is not authenticated")
    _require(_sha256(contract_path) == FROZEN_CONTRACT_SHA256
             and _sha256(audit_path) == FROZEN_AUDIT_FILE_SHA256,
             "contract_identity_mismatch", "complete v2 contract/audit bytes differ")
    _require(audit.get("next_gate") == "exact_quantized_pytorch_model",
             "identity_frontier", "audit does not authorize the exact model gate")
    _require(contract.get("schema") == "tinystories-1m-exact-input-contract-v2"
             and contract.get("status") == "authenticated",
             "identity_frontier", "v2 contract is not authenticated")
    audit_contract = audit.get("contract")
    _require(isinstance(audit_contract, dict) and audit_contract.get("sha256") == _sha256(contract_path),
             "identity_frontier", "audit is not bound to the supplied contract")
    required_model = {
        "name": "TinyStories-1M",
        "source_model_id": "roneneldan/TinyStories-1M",
        "source_revision": "ac533fb8b4f69c71894bf96badfe11e6294d9fcf",
        "architecture": "gpt_neo",
        "n_layer": 8,
        "hidden_size": 64,
        "n_head": 16,
        "head_dim": 4,
        "vocab_size": 50257,
        "max_context": 32,
    }
    _require(contract.get("model") == required_model and audit.get("model") == required_model,
             "model_identity_mismatch", "frozen model identity differs")
    required_reference = {
        "prompt_text": "Once upon a time",
        "prompt_tokens": [7454, 2402, 257, 640],
        "tokens": [11, 612, 373, 257, 1310, 2576, 3706, 20037,
                   13, 1375, 6151, 284, 711, 2354, 287, 262],
        "generation": "greedy top-1",
    }
    _require(contract.get("reference") == required_reference,
             "model_identity_mismatch", "frozen generation fixture differs")
    audit_fixture = audit.get("reference_fixture", {})
    _require(audit_fixture.get("prompt_ids") == required_reference["prompt_tokens"]
             and audit_fixture.get("expected_tokens") == required_reference["tokens"]
             and audit_fixture.get("fixed_reference_tokens") == required_reference["tokens"],
             "model_identity_mismatch", "authenticated reference fixture differs")
    deployed = contract.get("deployed_profile", {})
    _require(deployed.get("name") == "fixed_hardware_reference"
             and deployed.get("revision") == "df1fc45b2ffcb26fddc19cfd57621e7eedf6153f"
             and deployed.get("nonfinite_policy") == "reject_nonfinite_adapter_input"
             and audit.get("nonfinite_policy") == "reject_nonfinite_adapter_input",
             "model_identity_mismatch", "deployed executable identity differs")
    selection = contract.get("selection_authority", {})
    selection_path = _repo_root() / str(selection.get("path", ""))
    _require(selection_path.is_file() and (
        selection.get("sha256") == _sha256(selection_path)
        or historical_generation_artifact_path is not None
    ),
             "model_identity_mismatch", "accepted selection authority differs")

    fixed_authority = contract.get("semantic_authorities", {}).get("fixed_profile", {})
    audit_fixed = audit.get("semantic_receipts", {}).get("fixed_profile", {})
    profile_path = _repo_root() / FIXED_PROFILE_RELATIVE
    _require(fixed_authority.get("path") == str(FIXED_PROFILE_RELATIVE)
             and fixed_authority.get("sha256") == FROZEN_FIXED_PROFILE_SHA256
             and audit_fixed.get("sha256") == FROZEN_FIXED_PROFILE_SHA256
             and _sha256(profile_path) == FROZEN_FIXED_PROFILE_SHA256,
             "arithmetic_identity_mismatch", "fixed-profile identity differs")
    profile = _load_json(profile_path, "fixed hardware profile")
    _require(profile.get("profile_sha256") == FROZEN_PROFILE_SELF_SHA256
             and profile.get("profile_sha256") == canonical_sha256(
                 {key: value for key, value in profile.items() if key != "profile_sha256"}
             ), "arithmetic_identity_mismatch", "fixed-profile self-hash differs")
    _validate_arithmetic_identity(contract, audit, profile)

    package_contract = contract.get("package")
    audit_package = audit.get("package")
    _require(isinstance(package_contract, dict) and isinstance(audit_package, dict),
             "package_identity_mismatch", "package identity missing")
    canonical_origin = package_contract.get("origin")
    _require(isinstance(canonical_origin, str) and canonical_origin
             and audit_package.get("path") == canonical_origin,
             "package_identity_mismatch", "canonical package provenance differs")
    expected_files = package_contract.get("files")
    _require(isinstance(expected_files, dict) and audit_package.get("files") == expected_files,
             "package_identity_mismatch", "audit and contract package files differ")
    _require(package_path.is_dir()
             and {path.name for path in package_path.iterdir() if path.is_file()}
             == set(expected_files) | {"receipt.json"},
             "package_identity_mismatch", "package file set differs")
    for name, identity in expected_files.items():
        path = package_path / name
        _require(isinstance(identity, dict) and path.is_file()
                 and path.stat().st_size == identity.get("size")
                 and _sha256(path) == identity.get("sha256"),
                 "package_identity_mismatch", name)
    _require(package_contract.get("manifest_sha256") == expected_files["manifest.json"]["sha256"]
             and package_contract.get("sha256") == expected_files["weights.bin"]["sha256"],
             "package_identity_mismatch", "package aliases differ")
    receipt_path = package_path / "receipt.json"
    receipt = _load_json(receipt_path, "package receipt")
    _require(_sha256(receipt_path) == FROZEN_PACKAGE_RECEIPT_SHA256
             and receipt.get("files") == expected_files
             and receipt.get("manifest_sha256") == package_contract.get("manifest_sha256"),
             "package_identity_mismatch", "package receipt differs")
    certificate_path = _repo_root() / REACHABLE_CERTIFICATE_RELATIVE
    oracle_path = _repo_root() / FIXED_LOGITS_ORACLE_RELATIVE
    _require(certificate_path.is_file() and _sha256(certificate_path) == REACHABLE_CERTIFICATE_SHA256,
             "identity_frontier", "reachable-domain certificate artifact differs")
    _require(oracle_path.is_file() and _sha256(oracle_path) == FIXED_LOGITS_ORACLE_SHA256,
             "independent_logits_identity_mismatch", "fixed-logits oracle artifact differs")
    certificate = _load_json(certificate_path, "reachable-domain certificate")
    oracle = _load_json(oracle_path, "fixed-logits oracle")
    _validate_reachable_certificate(certificate, contract, audit)
    oracle_logits = _validate_fixed_logits_oracle(oracle, contract, audit)
    return contract, audit, profile, certificate, oracle, oracle_logits, {
        "canonical_origin": canonical_origin,
        "materialized_path": str(package_path),
        "content_alias_policy": "complete_authenticated_package_file_identity",
    }


def _tensor_images(manifest: Mapping[str, Any], weight_image: bytes, scale_image: bytes,
                   state_dict: Mapping[str, torch.Tensor]) -> tuple[
                       dict[str, torch.Tensor], dict[str, torch.Tensor], dict[str, torch.Tensor]
                   ]:
    codes: dict[str, torch.Tensor] = {}
    scales: dict[str, torch.Tensor] = {}
    parameters: dict[str, torch.Tensor] = {}
    tensors = manifest["tensors"]
    for name, descriptor in tensors.items():
        target = reference_adapter.package_name_to_gpt_neo_key(name)
        shape = tuple(descriptor["logical_shape"])
        if descriptor["format"] == "symmetric_int8_per_output":
            offset = int(descriptor["offset"])
            raw = weight_image[offset:offset + int(descriptor["nbytes"])]
            codes[name] = torch.frombuffer(bytearray(raw), dtype=torch.int8).clone().reshape(shape).to(torch.int64)
            scale_offset = int(descriptor["scale_offset"])
            scale_raw = scale_image[scale_offset:scale_offset + int(descriptor["scale_nbytes"])]
            float_scales = torch.frombuffer(bytearray(scale_raw), dtype=torch.float32).clone().to(torch.float64)
            rounded = torch.round(float_scales * (1 << Q_SCALE))
            _require(bool(torch.isfinite(rounded).all())
                     and bool(((rounded > 0) & (rounded < (1 << 24))).all()),
                     "arithmetic_identity_mismatch", f"{name}: Q8.24 scale range")
            materialized = rounded.to(torch.int64)
            scales[name] = materialized
        else:
            rounded = torch.round(state_dict[target].to(torch.float64) * (1 << Q_VALUE))
            _require(bool(torch.isfinite(rounded).all())
                     and bool(((rounded >= -(1 << 31)) & (rounded < (1 << 31))).all()),
                     "q16_range_violation", f"{name}: parameter does not fit signed Q16.16 int32")
            parameters[name] = rounded.to(torch.int64)
    for name, values in manifest["activation_scales"].items():
        rounded = torch.round(torch.tensor(values, dtype=torch.float64) * (1 << Q_SCALE))
        _require(bool(torch.isfinite(rounded).all())
                 and bool(((rounded > 0) & (rounded < (1 << 24))).all()),
                 "arithmetic_identity_mismatch", f"{name}: activation scale range")
        materialized = rounded.to(torch.int64)
        scales[f"activation::{name}"] = materialized
    return codes, scales, parameters


class _ExactFixedPointModel(torch.nn.Module):
    def __init__(self, codes: Mapping[str, torch.Tensor], scales: Mapping[str, torch.Tensor],
                 parameters: Mapping[str, torch.Tensor], finite_validator: Callable[[Any], None]) -> None:
        super().__init__()
        self._buffer_names: dict[str, str] = {}
        for prefix, values in (("code", codes), ("scale", scales), ("parameter", parameters)):
            for index, (name, tensor) in enumerate(sorted(values.items())):
                attribute = f"_{prefix}_{index}"
                self.register_buffer(attribute, tensor.to(torch.int64))
                self._buffer_names[f"{prefix}::{name}"] = attribute
        self.register_buffer("_gelu_lut", _gelu_table())
        self.register_buffer("_exp_lut", _exp_table())
        self._finite_validator = finite_validator

    def _buffer(self, prefix: str, name: str) -> torch.Tensor:
        return getattr(self, self._buffer_names[f"{prefix}::{name}"])

    def _activation_scale(self, name: str) -> torch.Tensor:
        return self._buffer("scale", f"activation::{name}")

    def _validate_input(self, input_ids: torch.Tensor) -> None:
        if not isinstance(input_ids, torch.Tensor):
            raise ExactModelError("nonfinite_adapter_input", "input_ids must be a tensor")
        try:
            self._finite_validator(input_ids.detach().cpu().tolist())
        except ValueError as error:
            raise ExactModelError("nonfinite_adapter_input", str(error)) from error
        _require(input_ids.dtype == torch.int64, "nonfinite_adapter_input", "token IDs must be torch.int64")
        _require(input_ids.ndim == 2 and input_ids.shape[0] == 1,
                 "input_shape_mismatch", "expected one [1, context] token batch")
        _require(1 <= input_ids.shape[1] <= MAX_CONTEXT,
                 "context_capacity_exceeded", "context length must be between 1 and 32")
        _require(bool(((input_ids >= 0) & (input_ids < 50257)).all()),
                 "token_id_out_of_range", "token IDs must be in [0, 50257)")

    def _embedding(self, name: str, rows: torch.Tensor) -> torch.Tensor:
        codes = self._buffer("code", name)[rows]
        scales = self._buffer("scale", name)[rows]
        return round_shift_signed(codes * scales.unsqueeze(-1), Q_SCALE - Q_VALUE)

    def _gemv(self, values: torch.Tensor, weight: str, bias: str | None,
              module: str) -> tuple[torch.Tensor, tuple[torch.Tensor, ...], torch.Tensor]:
        original_shape = values.shape[:-1]
        flattened = values.reshape(-1, values.shape[-1])
        input_scale = self._activation_scale(f"{module}.input")
        input_codes, input_dequantized = activation_qdq(flattened, input_scale)
        scaled_input = input_codes * input_scale
        accumulator = serial_gemv(scaled_input, self._buffer("code", weight))
        real_q16 = round_shift_signed(
            accumulator * self._buffer("scale", weight), 2 * Q_SCALE - Q_VALUE
        )
        if bias is not None:
            real_q16 = real_q16 + self._buffer("parameter", bias)
        output_scale = self._activation_scale(f"{module}.output")
        output_codes, output_dequantized = activation_qdq(real_q16, output_scale)
        result = output_dequantized.reshape(original_shape + (output_dequantized.shape[-1],))
        observations = (
            input_codes, input_scale, input_dequantized,
            output_codes, output_scale, output_dequantized,
        )
        return result, observations, accumulator

    def _fixed_gelu(self, values_q16: torch.Tensor) -> torch.Tensor:
        q12 = torch.clamp(round_shift_signed(values_q16, 4), -32768, 32767)
        biased = q12 + 32768
        index = torch.bitwise_right_shift(biased, 3)
        fraction = torch.bitwise_and(biased, 7)
        upper = torch.minimum(index + 1, torch.full_like(index, 8191))
        result_q12 = self._gelu_lut[index] + torch.bitwise_right_shift(
            (self._gelu_lut[upper] - self._gelu_lut[index]) * fraction, 3
        )
        return torch.bitwise_left_shift(result_q12, 4)

    def _attention(self, query: torch.Tensor, key: torch.Tensor, value: torch.Tensor) -> torch.Tensor:
        length = query.shape[1]
        heads: list[torch.Tensor] = []
        for head in range(16):
            columns = slice(head * 4, head * 4 + 4)
            q_head = query[:, :, columns]
            k_head = key[:, :, columns]
            v_head = value[:, :, columns]
            positions: list[torch.Tensor] = []
            for position in range(length):
                products = q_head[:, position:position + 1, :] * k_head[:, :position + 1, :]
                scores = torch.bitwise_right_shift(torch.sum(products, dim=-1, dtype=torch.int64), 24)
                maxima = torch.amax(scores, dim=-1, keepdim=True)
                delta = torch.clamp(scores - maxima, -4096, 0)
                table_index = torch.minimum(4096 + delta, torch.full_like(delta, 4095))
                probabilities = torch.where(delta == 0, torch.full_like(delta, 1 << 20),
                                            self._exp_lut[table_index])
                denominator = torch.sum(probabilities, dim=-1, keepdim=True, dtype=torch.int64)
                numerator = torch.sum(probabilities.unsqueeze(-1) * v_head[:, :position + 1, :],
                                      dim=1, dtype=torch.int64)
                correction = torch.where(numerator < 0, -torch.div(denominator, 2, rounding_mode="floor"),
                                         torch.div(denominator, 2, rounding_mode="floor"))
                positions.append(_trunc_divide(numerator + correction, denominator))
            heads.append(torch.stack(positions, dim=1))
        return torch.cat(heads, dim=-1)

    def _execute(self, input_ids: torch.Tensor) -> tuple[
        torch.Tensor, tuple[torch.Tensor, ...], tuple[torch.Tensor, ...],
        tuple[torch.Tensor, ...], tuple[torch.Tensor, ...]
    ]:
        length = input_ids.shape[1]
        positions = torch.arange(length, dtype=torch.int64, device=input_ids.device).unsqueeze(0)
        x = self._embedding("token_embedding.weight", input_ids)
        x = x + self._embedding("position_embedding.weight", positions)
        block_zero: tuple[torch.Tensor, ...] | None = None
        qdq_observations: list[torch.Tensor] = []
        gemv_accumulators: list[torch.Tensor] = []
        nonlinear_observations: list[torch.Tensor] = []
        for layer in range(8):
            block = f"blocks.{layer}"
            source = f"transformer.h.{layer}"
            block_input = x
            normalized = _fixed_layer_norm(
                x, self._buffer("parameter", f"{block}.ln1.weight"),
                self._buffer("parameter", f"{block}.ln1.bias"),
            )
            nonlinear_observations.append(normalized)
            query, observed, accumulator = self._gemv(
                normalized, f"{block}.attn.q.weight", None,
                f"{source}.attn.attention.q_proj"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            key, observed, accumulator = self._gemv(
                normalized, f"{block}.attn.k.weight", None,
                f"{source}.attn.attention.k_proj"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            value, observed, accumulator = self._gemv(
                normalized, f"{block}.attn.v.weight", None,
                f"{source}.attn.attention.v_proj"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            context = self._attention(query, key, value)
            nonlinear_observations.append(context)
            attention, observed, accumulator = self._gemv(
                context, f"{block}.attn.out.weight", f"{block}.attn.out.bias",
                f"{source}.attn.attention.out_proj"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            residual_attention = x + attention
            normalized_2 = _fixed_layer_norm(
                residual_attention, self._buffer("parameter", f"{block}.ln2.weight"),
                self._buffer("parameter", f"{block}.ln2.bias"),
            )
            nonlinear_observations.append(normalized_2)
            hidden, observed, accumulator = self._gemv(
                normalized_2, f"{block}.mlp.fc.weight", f"{block}.mlp.fc.bias",
                f"{source}.mlp.c_fc"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            activated = self._fixed_gelu(hidden)
            nonlinear_observations.append(activated)
            projected, observed, accumulator = self._gemv(
                activated, f"{block}.mlp.proj.weight", f"{block}.mlp.proj.bias",
                f"{source}.mlp.c_proj"
            )
            qdq_observations.extend(observed)
            gemv_accumulators.append(accumulator)
            x = residual_attention + projected
            if layer == 0:
                last = length - 1
                block_zero = (
                    block_input[0, last], normalized[0, last], query[0, last].reshape(16, 4),
                    key[0, last].reshape(16, 4), value[0, last].reshape(16, 4),
                    attention[0, last], residual_attention[0, last], normalized_2[0, last],
                    hidden[0, last], activated[0, last], projected[0, last], x[0, last],
                )
        normalized = _fixed_layer_norm(
            x, self._buffer("parameter", "final_ln.weight"), self._buffer("parameter", "final_ln.bias")
        )
        nonlinear_observations.append(normalized)
        input_scale = self._activation_scale("lm_head.input")
        input_codes, input_dequantized = activation_qdq(normalized.reshape(-1, 64), input_scale)
        qdq_observations.extend((input_codes, input_scale, input_dequantized))
        accumulator = serial_gemv(
            input_codes * input_scale, self._buffer("code", "token_embedding.weight")
        )
        gemv_accumulators.append(accumulator)
        logits = round_shift_signed(
            accumulator * self._buffer("scale", "token_embedding.weight"), 2 * Q_SCALE - Q_VALUE
        ).reshape(1, length, 50257)
        assert block_zero is not None
        return (
            logits, block_zero, tuple(qdq_observations),
            tuple(gemv_accumulators), tuple(nonlinear_observations),
        )

    def forward(self, input_ids: torch.Tensor) -> torch.Tensor:
        if not torch.compiler.is_compiling():
            self._validate_input(input_ids)
        return self._execute(input_ids)[0]


class _TraceOutputs(torch.nn.Module):
    def __init__(self, model: _ExactFixedPointModel) -> None:
        super().__init__()
        self.model = model

    def forward(self, input_ids: torch.Tensor) -> tuple[torch.Tensor, ...]:
        logits, checkpoints, qdq, accumulators, nonlinear = self.model._execute(input_ids)
        return (logits,) + checkpoints + qdq + accumulators + nonlinear


def _tensor_observation(tensor: torch.Tensor, semantic_dtype: str) -> dict[str, Any]:
    payload = {
        "shape": list(tensor.shape),
        "dtype": semantic_dtype,
        "values": tensor.detach().cpu().tolist(),
    }
    return {"shape": payload["shape"], "sha256": canonical_sha256(payload)}


class ExactModelBundle:
    def __init__(self, *, contract: dict[str, Any], audit: dict[str, Any], profile: dict[str, Any],
                 certificate: dict[str, Any], oracle: dict[str, Any], oracle_logits: torch.Tensor,
                 manifest: dict[str, Any], model: _ExactFixedPointModel, receipt: dict[str, Any]) -> None:
        self.contract = contract
        self.audit = audit
        self.profile = profile
        self.certificate = certificate
        self.oracle = oracle
        self.oracle_logits = oracle_logits
        self.manifest = manifest
        self.model = model
        self.receipt = receipt
        self.export_verification: dict[str, Any] = {}

    def trace(self, input_ids: torch.Tensor) -> dict[str, Any]:
        self.model._validate_input(input_ids)
        with torch.no_grad():
            logits, tensors, qdq_tensors, accumulators, nonlinear_tensors = self.model._execute(input_ids)
        checkpoints: dict[str, dict[str, Any]] = {}
        for (name, shape), tensor in zip(CHECKPOINT_SHAPES.items(), tensors, strict=True):
            _require(tuple(tensor.shape) == shape, "trace_schema_mismatch", name)
            payload = {"shape": list(shape), "dtype": "signed_q16.16", "values": tensor.cpu().tolist()}
            checkpoints[name] = {**payload, "sha256": canonical_sha256(payload)}
        oracle: dict[str, Any] = {
            "status": "runtime_authenticated_not_board_checkpoint_authenticated",
            "profile": "fixed_hardware_reference",
            "block_index": 0,
            "token_index": input_ids.shape[1] - 1,
            "prompt_tokens": input_ids[0].cpu().tolist(),
            "next_token": int(torch.argmax(logits[0, -1])),
            "checkpoint_order": list(CHECKPOINT_SHAPES),
            "checkpoints": checkpoints,
        }
        oracle["sha256"] = canonical_sha256(oracle)
        _require(len(qdq_tensors) == len(QDQ_BOUNDARY_NAMES) * 3,
                 "trace_schema_mismatch", "97 Q/DQ observations are required")
        _require(len(accumulators) == len(GEMV_NAMES),
                 "trace_schema_mismatch", "49 GEMV accumulators are required")
        _require(len(nonlinear_tensors) == len(NONLINEAR_BOUNDARY_NAMES),
                 "trace_schema_mismatch", "33 nonlinear observations are required")
        qdq_boundaries = []
        for index, name in enumerate(QDQ_BOUNDARY_NAMES):
            codes, scales, dequantized = qdq_tensors[index * 3:index * 3 + 3]
            qdq_boundaries.append({
                "name": name,
                "execution_index": index,
                "codes_shape": list(codes.shape),
                "codes_sha256": _tensor_observation(codes, "signed_int8_codes")["sha256"],
                "scales_shape": list(scales.shape),
                "scales_sha256": _tensor_observation(scales, "unsigned_q8.24")["sha256"],
                "dequantized_shape": list(dequantized.shape),
                "dequantized_sha256": _tensor_observation(dequantized, "signed_q16.16")["sha256"],
            })
        accumulator_observations = {
            name: _tensor_observation(tensor, "signed_int64_serial_accumulator")
            for name, tensor in zip(GEMV_NAMES, accumulators, strict=True)
        }
        nonlinear_observations = {
            name: _tensor_observation(tensor, "signed_q16.16")
            for name, tensor in zip(NONLINEAR_BOUNDARY_NAMES, nonlinear_tensors, strict=True)
        }
        q_input_codes, q_input_scale, _ = qdq_tensors[0:3]
        q_output_codes, q_output_scale, q_output = qdq_tensors[3:6]
        q_accumulator = accumulators[0]
        q_weight_scale = self.model._buffer("scale", "blocks.0.attn.q.weight")
        _require(torch.equal(q_output[-1].reshape(16, 4), tensors[2]),
                 "trace_arithmetic_mismatch", "observable q projection differs")
        return {
            **oracle,
            "trace_sha256": oracle["sha256"],
            "qdq_boundaries": qdq_boundaries,
            "gemv_accumulators": accumulator_observations,
            "nonlinear_boundaries": nonlinear_observations,
            "arithmetic": {
                "value_format": "signed Q16.16",
                "accumulator": "signed_int64_serial_wrap",
                "rounding": "nearest_ties_away_from_zero",
                "saturation": [-128, 127],
                "q_proj.input": {
                    "codes": q_input_codes[-1].cpu().tolist(),
                    "scales": q_input_scale.cpu().tolist(),
                    "scale_format": "unsigned Q8.24",
                },
                "q_proj.accumulator": {
                    "values": q_accumulator[-1].cpu().tolist(),
                    "order": "ascending_input_index",
                    "width_bits": 64,
                    "overflow": "twos_complement_wrap",
                },
                "q_proj.weight_scale": {
                    "values": q_weight_scale.cpu().tolist(),
                    "round_shift": 32,
                },
                "q_proj.output": {
                    "codes": q_output_codes[-1].cpu().tolist(),
                    "scales": q_output_scale.cpu().tolist(),
                    "dequantized": q_output[-1].cpu().tolist(),
                    "scale_format": "unsigned Q8.24",
                },
            },
        }


def _load_exact_model(contract_path: Path, package_path: Path, model_path: Path, *,
                      historical_generation_artifact_path: Path | None = None) -> ExactModelBundle:
    """Materialize the exact model after the caller-selected identity gate."""

    contract_path = Path(contract_path)
    package_path = Path(package_path)
    model_path = Path(model_path)
    contract, audit, profile, certificate, oracle, oracle_logits, package_location = _authenticate_inputs(
        contract_path, package_path,
        historical_generation_artifact_path=historical_generation_artifact_path,
    )
    manifest = _load_json(package_path / "manifest.json", "package manifest")
    reference_adapter._validate_config(model_path, manifest)
    target_shapes = {
        reference_adapter.package_name_to_gpt_neo_key(name): tuple(descriptor["logical_shape"])
        for name, descriptor in manifest["tensors"].items()
    }
    target_shapes["lm_head.weight"] = target_shapes["transformer.wte.weight"]
    weight_image = (package_path / "weights.bin").read_bytes()
    scale_image = (package_path / "scales.bin").read_bytes()
    state_dict, mapping = reference_adapter.reconstruct_state_dict(
        manifest, weight_image, scale_image, target_shapes
    )
    codes, scales, parameters = _tensor_images(manifest, weight_image, scale_image, state_dict)
    model = _ExactFixedPointModel(codes, scales, parameters, _load_finite_validator()).eval()
    prompt = torch.tensor([contract["reference"]["prompt_tokens"]], dtype=torch.int64)
    with torch.no_grad():
        authenticated_logits = model(prompt)[0, -1]
    _require(torch.equal(authenticated_logits, oracle_logits),
             "independent_logits_mismatch", "adapter logits differ from pinned fixed reference")
    receipt: dict[str, Any] = {
        "schema": "tinystories-1m-exact-package-model-v1",
        "status": "authenticated_fixed_point_model",
        "identity": {
            "contract_sha256": _sha256(contract_path),
            "audit_sha256": audit["sha256"],
            "fixed_profile_sha256": FROZEN_FIXED_PROFILE_SHA256,
            "reachable_certificate_sha256": REACHABLE_CERTIFICATE_SHA256,
            "fixed_logits_oracle_sha256": FIXED_LOGITS_ORACLE_SHA256,
            "package_manifest_sha256": contract["package"]["manifest_sha256"],
            "package_weights_sha256": contract["package"]["sha256"],
        },
        "execution": {
            "domain": "integers_only_after_materialization",
            "activation_codes": "signed_int8_saturated",
            "value_format": "signed_q16.16_int64_tensor",
            "scale_format": "unsigned_q8.24_int64_tensor",
            "gemv_accumulation": "ascending_input_index_signed_int64_twos_complement_wrap",
            "activation_rounding": "nearest_ties_away_from_zero",
        },
        "package_location": package_location,
        "tensor_mapping": mapping,
        "activation_boundary_count": len(manifest["activation_scales"]),
        "independent_logits_oracle": {
            "status": "full_logits_bit_exact",
            "canonical_sha256": oracle["logits"]["canonical_sha256"],
            "little_endian_int64_sha256": oracle["logits"]["little_endian_int64_sha256"],
            "reference_revision": oracle["identity"]["reference_revision"],
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return ExactModelBundle(
        contract=contract, audit=audit, profile=profile, certificate=certificate,
        oracle=oracle, oracle_logits=oracle_logits, manifest=manifest, model=model, receipt=receipt
    )


def load_exact_model(contract_path: Path, package_path: Path, model_path: Path) -> ExactModelBundle:
    """Authenticate all current identities, materialize integers, and construct the exact model."""

    return _load_exact_model(
        contract_path, package_path, model_path
    )


def load_successor_exact_model(
    contract_path: Path,
    package_path: Path,
    model_path: Path,
    historical_generation_artifact_path: Path,
) -> ExactModelBundle:
    """Load a post-boundary successor after validating its historical artifact.

    Validation is deliberately the first operation. All package, arithmetic,
    certificate, and oracle identity gates remain mandatory after the exact
    frozen artifact authorizes the historical-selection exception.
    """

    _validate_frozen_generation_artifact(historical_generation_artifact_path)
    return _load_exact_model(
        contract_path, package_path, model_path,
        historical_generation_artifact_path=historical_generation_artifact_path,
    )


def _named_tensor_state_sha256(values: Mapping[str, torch.Tensor]) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(values.items()):
        value = tensor.detach().cpu().contiguous()
        digest.update(name.encode())
        digest.update(str(value.dtype).encode())
        digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
        digest.update(value.numpy().tobytes())
    return digest.hexdigest()


def exported_program_identity(exported: torch.export.ExportedProgram) -> dict[str, Any]:
    """Canonical graph/signature/state identity independent of zip timestamps."""

    graph_code = exported.graph_module.code
    signature = str(exported.graph_signature)
    constraints = sorted((str(key), str(value)) for key, value in exported.range_constraints.items())
    tensor_constants = {
        name: value for name, value in exported.constants.items()
        if isinstance(value, torch.Tensor)
    }
    non_tensor_constants = {
        name: repr(value) for name, value in exported.constants.items()
        if not isinstance(value, torch.Tensor)
    }
    identity: dict[str, Any] = {
        "schema": "torch-exported-program-canonical-identity-v1",
        "graph_code_sha256": hashlib.sha256(graph_code.encode()).hexdigest(),
        "graph_signature_sha256": hashlib.sha256(signature.encode()).hexdigest(),
        "range_constraints_sha256": canonical_sha256(constraints),
        "state_dict_sha256": _named_tensor_state_sha256(exported.state_dict),
        "tensor_constants_sha256": _named_tensor_state_sha256(tensor_constants),
        "non_tensor_constants_sha256": canonical_sha256(non_tensor_constants),
    }
    identity["program_sha256"] = canonical_sha256(identity)
    return identity


def export_exact_program(bundle: ExactModelBundle) -> torch.export.ExportedProgram:
    """Verify exported checkpoints, then return the logits-only exact program."""

    prompt = torch.tensor([bundle.contract["reference"]["prompt_tokens"]], dtype=torch.int64)
    with torch.no_grad():
        eager_logits, eager_checkpoints, eager_qdq, eager_accumulators, eager_nonlinear = (
            bundle.model._execute(prompt)
        )
    _require(torch.equal(eager_logits[0, -1], bundle.oracle_logits),
             "independent_logits_mismatch", "eager logits differ from pinned oracle")
    trace_export = torch.export.export(_TraceOutputs(bundle.model).eval(), (prompt,), strict=False)
    with torch.no_grad():
        replay = trace_export.module()(prompt)
    replay_logits = replay[0]
    checkpoint_end = 1 + len(CHECKPOINT_SHAPES)
    qdq_end = checkpoint_end + len(QDQ_BOUNDARY_NAMES) * 3
    accumulator_end = qdq_end + len(GEMV_NAMES)
    nonlinear_end = accumulator_end + len(NONLINEAR_BOUNDARY_NAMES)
    _require(len(replay) == nonlinear_end, "export_trace_schema_mismatch", str(len(replay)))
    replay_checkpoints = replay[1:checkpoint_end]
    replay_qdq = replay[checkpoint_end:qdq_end]
    replay_accumulators = replay[qdq_end:accumulator_end]
    replay_nonlinear = replay[accumulator_end:nonlinear_end]
    checkpoint_hashes: dict[str, str] = {}
    for (name, shape), eager, exported in zip(
        CHECKPOINT_SHAPES.items(), eager_checkpoints, replay_checkpoints, strict=True
    ):
        _require(torch.equal(eager, exported), "export_checkpoint_mismatch", name)
        payload = {"shape": list(shape), "dtype": "signed_q16.16", "values": eager.cpu().tolist()}
        checkpoint_hashes[name] = canonical_sha256(payload)
    _require(torch.equal(eager_logits, replay_logits), "export_logits_mismatch", "trace export logits")
    qdq_hashes: dict[str, dict[str, str]] = {}
    for index, name in enumerate(QDQ_BOUNDARY_NAMES):
        eager_group = eager_qdq[index * 3:index * 3 + 3]
        replay_group = replay_qdq[index * 3:index * 3 + 3]
        _require(all(torch.equal(eager, actual)
                     for eager, actual in zip(eager_group, replay_group, strict=True)),
                 "export_qdq_mismatch", name)
        qdq_hashes[name] = {
            semantic: _tensor_observation(tensor, dtype)["sha256"]
            for semantic, tensor, dtype in zip(
                ("codes", "scales", "dequantized"), eager_group,
                ("signed_int8_codes", "unsigned_q8.24", "signed_q16.16"), strict=True,
            )
        }
    accumulator_hashes = {}
    for name, eager, actual in zip(
        GEMV_NAMES, eager_accumulators, replay_accumulators, strict=True
    ):
        _require(torch.equal(eager, actual), "export_accumulator_mismatch", name)
        accumulator_hashes[name] = _tensor_observation(
            eager, "signed_int64_serial_accumulator"
        )["sha256"]
    nonlinear_hashes = {}
    for name, eager, actual in zip(
        NONLINEAR_BOUNDARY_NAMES, eager_nonlinear, replay_nonlinear, strict=True
    ):
        _require(torch.equal(eager, actual), "export_nonlinear_mismatch", name)
        nonlinear_hashes[name] = _tensor_observation(eager, "signed_q16.16")["sha256"]
    logits_export = torch.export.export(bundle.model.eval(), (prompt,), strict=False)
    with torch.no_grad():
        logits_replay = logits_export.module()(prompt)
    _require(torch.equal(eager_logits, logits_replay), "export_logits_mismatch", "logits-only export")
    bundle.export_verification = {
        "status": "matched",
        "checkpoint_sha256": checkpoint_hashes,
        "qdq_boundary_sha256": qdq_hashes,
        "gemv_accumulator_sha256": accumulator_hashes,
        "nonlinear_boundary_sha256": nonlinear_hashes,
        "final_logits_status": "matched",
        "final_logits_sha256": hashlib.sha256(eager_logits.cpu().numpy().astype("<i8").tobytes()).hexdigest(),
        "final_logits_canonical_sha256": canonical_sha256(eager_logits[0, -1].cpu().tolist()),
        "independent_oracle_status": "full_logits_bit_exact",
        "exported_program": exported_program_identity(logits_export),
    }
    return logits_export


def export_program_with_package(
    model_path: str | Path,
    package_path: str | Path,
    contract_path: str | Path,
) -> torch.export.ExportedProgram:
    """Authenticate explicit package inputs before exposing the exact export."""

    if not model_path or not package_path or not contract_path:
        raise ExactModelError(
            "package_frontend_inputs_missing",
            "model, package, and frozen contract paths are required",
        )
    # The authenticated contract predates later edits to its selection-spec
    # document.  The immutable generation receipt is the explicit authority
    # for that historical selection, so use its validated exception rather
    # than silently accepting a changed document hash.
    historical = _repo_root() / FROZEN_GENERATION_ARTIFACT_RELATIVE
    if historical.is_file():
        bundle = load_successor_exact_model(
            Path(contract_path), Path(package_path), Path(model_path), historical
        )
    else:
        bundle = load_exact_model(Path(contract_path), Path(package_path), Path(model_path))
    return export_exact_program(bundle)
