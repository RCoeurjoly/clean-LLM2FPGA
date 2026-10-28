#!/usr/bin/env python3
"""Freeze the exact two-token TinyStories-1M model-level boundary oracle.

This module observes the existing authenticated fixed-point implementation.  It
does not reimplement or alter its arithmetic.  In particular, the second token
is selected from the first invocation's logits and appended internally; callers
cannot supply that token or any intermediate hidden state.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from TinyStories import model_adapter_exact_package as exact_adapter  # noqa: E402


SCHEMA = "tinystories-1m-fixed-point-model-token-step-oracle-v1"
STATUS = "authenticated_exact_two_token_model_oracle"
PROMPT_TOKENS = [7454, 2402, 257, 640]
STEP_COUNT = 2
EXPECTED_STEP_SHA256 = (
    "d65e41b23585938ce5118128adeaf05612bcdf9772fcf9170f16d659c2d7beae",
    "ad18da108b924945d908b12f58494904080af984224e536a6b17d6f8d3dbd8c4",
)
EXPECTED_MODEL_CONFIG_SHA256 = (
    "ff74c30d5ebb5ab1da0f2ea479adf7197c504b42b5522a858c334ab91ed4958c"
)
MODEL_CONTRACT = {
    "name": "TinyStories-1M",
    "architecture": "gpt_neo",
    "n_layer": 8,
    "hidden_size": 64,
    "n_head": 16,
    "head_dim": 4,
    "vocab_size": 50257,
    "max_context": 32,
    "tie_word_embeddings": True,
}
BOUNDARY_ORDER = (
    "embedding",
    "transformer.h.0",
    "transformer.h.1",
    "transformer.h.2",
    "transformer.h.3",
    "transformer.h.4",
    "transformer.h.5",
    "transformer.h.6",
    "transformer.h.7",
    "transformer.ln_f",
    "lm_head",
    "greedy_selection",
)
FIXTURE_KEYS = {
    "schema",
    "status",
    "scope",
    "identity",
    "model_contract",
    "arithmetic_contract",
    "token_contract",
    "boundary_contract",
    "steps",
    "claims",
    "artifact_sha256",
}
ARITHMETIC_CONTRACT = {
    "value_format": "signed Q16.16",
    "scale_format": "unsigned Q8.24",
    "weights": "symmetric per-output INT8",
    "activations": "symmetric per-channel INT8",
    "gemv_accumulator": "signed 64-bit serial accumulator",
    "gemv_order": "ascending_input_index",
    "overflow": "twos_complement_wrap",
    "activation_rounding": "nearest_ties_away_from_zero",
}
TOKEN_CONTRACT_KEYS = {
    "initial_context",
    "step_count",
    "host_inputs",
    "forbidden_host_inputs",
    "feedback_rule",
    "greedy_tie_break",
}
STEP_KEYS = {
    "step_index",
    "context_tokens",
    "context_length",
    "context_sha256",
    "input_token_source",
    "feedback_token",
    "embedding",
    "transformer_blocks",
    "final_layer_norm",
    "lm_head",
    "selected_token",
    "predecessor",
    "boundary_order",
    "step_sha256",
}
IDENTITY_KEYS = {
    "contract_file_sha256",
    "generation_file_sha256",
    "generation_artifact_sha256",
    "package_manifest_sha256",
    "package_weights_sha256",
    "package_scales_sha256",
    "model_config_sha256",
    "exact_adapter_sha256",
    "capture_source_sha256",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_CONTRACT = ROOT / "artifacts/reference/tinystories-1m-exact-input-contract.json"
DEFAULT_GENERATION = ROOT / "artifacts/reference/tinystories-1m-exact-generation.json"
DEFAULT_PACKAGE = Path(
    "/home/roland/kev-gpt/.worktrees/kintex-selftest/model_packages/tinystories-1m"
)
DEFAULT_MODEL = Path(
    "/home/roland/.cache/huggingface/hub/"
    "models--roneneldan--TinyStories-1M/snapshots/"
    "77f1b168e219585646439073245fe87e56b3023e"
)
DEFAULT_OUTPUT = (
    ROOT
    / "artifacts/reference/tinystories-1m-fixed-point-model-token-step-oracle.json"
)


class ModelTokenStepOracleError(ValueError):
    """The model-level oracle is incomplete, injected, or unauthenticated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ModelTokenStepOracleError(code, message)


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelTokenStepOracleError("invalid_json", f"{path}: {error}") from error
    _require(isinstance(value, dict), "invalid_json", f"{path} must contain an object")
    return value


def _tensor_record(value: torch.Tensor, semantic_dtype: str) -> dict[str, Any]:
    tensor = value.detach().cpu().to(torch.int64).contiguous()
    payload = {
        "shape": list(tensor.shape),
        "dtype": semantic_dtype,
        "values": tensor.tolist(),
    }
    raw = tensor.numpy().astype("<i8", copy=False).tobytes()
    return {
        "shape": payload["shape"],
        "dtype": semantic_dtype,
        "sha256": canonical_sha256(payload),
        "little_endian_int64_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _buffer_record(value: torch.Tensor, semantic_dtype: str) -> dict[str, Any]:
    tensor = value.detach().cpu().to(torch.int64).contiguous()
    raw = tensor.numpy().astype("<i8", copy=False).tobytes()
    payload = {
        "shape": list(tensor.shape),
        "dtype": semantic_dtype,
        "little_endian_int64_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }
    return {**payload, "record_sha256": canonical_sha256(payload)}


def _with_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    payload = dict(value)
    return {**payload, field: canonical_sha256(payload)}


def _boundary_contract() -> dict[str, Any]:
    return {
        "order": list(BOUNDARY_ORDER),
        "transformer_block_count": 8,
        "tensor_hash": "canonical JSON over shape, semantic dtype, and exact int64 values",
        "raw_hash": "SHA-256 over contiguous little-endian int64 bytes",
        "lm_head_weight_source": "token_embedding.weight",
    }


def _load_generation_verifier(path: Path) -> Any:
    verifier = ROOT / "scripts/comparison/verify_tinystories_1m_exact_generation.py"
    spec = importlib.util.spec_from_file_location(
        "tinystories_exact_generation_authority", verifier
    )
    _require(
        spec is not None and spec.loader is not None,
        "generation_authority_unavailable",
        str(verifier),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    artifact = _load_json(path)
    try:
        module.validate_artifact(artifact, ROOT)
    except ValueError as error:
        raise ModelTokenStepOracleError(
            "generation_authority_mismatch", str(error)
        ) from error
    return artifact


def _capture_execution(
    model: exact_adapter._ExactFixedPointModel, context_tokens: Sequence[int]
) -> tuple[
    torch.Tensor,
    list[tuple[str, torch.Tensor]],
    list[tuple[torch.Tensor, torch.Tensor]],
]:
    """Observe embedding and LayerNorm crossings around the unchanged model."""

    embedding_calls: list[tuple[str, torch.Tensor]] = []
    layer_norm_calls: list[tuple[torch.Tensor, torch.Tensor]] = []
    original_embedding = model._embedding
    original_layer_norm = exact_adapter._fixed_layer_norm

    def observed_embedding(name: str, rows: torch.Tensor) -> torch.Tensor:
        result = original_embedding(name, rows)
        embedding_calls.append((name, result.detach().clone()))
        return result

    def observed_layer_norm(
        values: torch.Tensor, gamma: torch.Tensor, beta: torch.Tensor
    ) -> torch.Tensor:
        result = original_layer_norm(values, gamma, beta)
        layer_norm_calls.append((values.detach().clone(), result.detach().clone()))
        return result

    model.__dict__["_embedding"] = observed_embedding
    exact_adapter._fixed_layer_norm = observed_layer_norm
    try:
        input_ids = torch.tensor([list(context_tokens)], dtype=torch.int64)
        with torch.no_grad():
            logits = model._execute(input_ids)[0]
    finally:
        model.__dict__.pop("_embedding", None)
        exact_adapter._fixed_layer_norm = original_layer_norm

    _require(
        [name for name, _ in embedding_calls]
        == ["token_embedding.weight", "position_embedding.weight"],
        "embedding_capture_mismatch",
        str([name for name, _ in embedding_calls]),
    )
    _require(
        len(layer_norm_calls) == MODEL_CONTRACT["n_layer"] * 2 + 1,
        "layer_norm_capture_mismatch",
        str(len(layer_norm_calls)),
    )
    return logits, embedding_calls, layer_norm_calls


def _select_greedy(logits: torch.Tensor) -> tuple[int, int]:
    values = logits.detach().cpu().to(torch.int64).flatten()
    maximum = torch.max(values)
    tied = torch.nonzero(values == maximum, as_tuple=False).flatten()
    return int(torch.min(tied)), int(tied.numel())


def _capture_step(
    model: exact_adapter._ExactFixedPointModel,
    context_tokens: Sequence[int],
    step_index: int,
    predecessor_step: Mapping[str, Any],
) -> dict[str, Any]:
    logits, embeddings, layer_norms = _capture_execution(model, context_tokens)
    token_embedding = embeddings[0][1]
    position_embedding = embeddings[1][1]
    embedding_sum = layer_norms[0][0]
    _require(
        torch.equal(token_embedding + position_embedding, embedding_sum),
        "embedding_sum_mismatch",
        str(step_index),
    )

    embedding = _with_hash(
        {
            "token": _tensor_record(token_embedding, "signed_q16.16"),
            "position": _tensor_record(position_embedding, "signed_q16.16"),
            "sum": _tensor_record(embedding_sum, "signed_q16.16"),
        },
        "boundary_sha256",
    )
    blocks = []
    block_output_tensors: list[torch.Tensor] = []
    for block_index in range(MODEL_CONTRACT["n_layer"]):
        block_input = layer_norms[block_index * 2][0]
        if block_index + 1 < MODEL_CONTRACT["n_layer"]:
            block_output = layer_norms[(block_index + 1) * 2][0]
        else:
            block_output = layer_norms[-1][0]
        block_output_tensors.append(block_output)
        blocks.append(
            _with_hash(
                {
                    "block_index": block_index,
                    "input": _tensor_record(block_input, "signed_q16.16"),
                    "output": _tensor_record(block_output, "signed_q16.16"),
                },
                "boundary_sha256",
            )
        )

    final_layer_norm = _with_hash(
        {
            "input": _tensor_record(layer_norms[-1][0], "signed_q16.16"),
            "output": _tensor_record(layer_norms[-1][1], "signed_q16.16"),
        },
        "boundary_sha256",
    )
    last_logits = _tensor_record(logits[0, -1], "signed_fixed_point_logits")
    full_context_logits = _tensor_record(logits, "signed_fixed_point_logits")
    selected_token, tie_count = _select_greedy(logits[0, -1])
    lm_head = _with_hash(
        {
            "input": final_layer_norm["output"],
            "last_logits": last_logits,
            "full_context_logits": full_context_logits,
            "weight_source": "token_embedding.weight",
            "weight_codes": _buffer_record(
                model._buffer("code", "token_embedding.weight"),
                "signed_int8_codes_stored_as_int64",
            ),
            "weight_scales": _buffer_record(
                model._buffer("scale", "token_embedding.weight"), "unsigned_q8.24"
            ),
        },
        "boundary_sha256",
    )
    selected_payload = {
        "value": selected_token,
        "tie_count": tie_count,
        "tie_breaking": "smallest_token_id_among_equal_maxima",
    }
    selected = {
        **selected_payload,
        "sha256": canonical_sha256(
            {"dtype": "token_id", "value": selected_token}
        ),
        "selection_sha256": canonical_sha256(selected_payload),
    }

    expected_context = predecessor_step.get("context_sha256")
    context_sha256 = canonical_sha256(list(context_tokens))
    _require(
        expected_context == context_sha256,
        "predecessor_context_mismatch",
        str(step_index),
    )
    expected_evidence = predecessor_step.get("evidence", {})
    _require(
        expected_evidence.get("logits_sha256") == last_logits["sha256"]
        and expected_evidence.get("full_context_logits_sha256")
        == full_context_logits["sha256"],
        "predecessor_logits_mismatch",
        str(step_index),
    )
    _require(
        predecessor_step.get("selected_token") == selected_token
        and predecessor_step.get("tie_count") == tie_count,
        "predecessor_token_mismatch",
        str(step_index),
    )
    block_zero_checkpoints = expected_evidence.get("checkpoint_sha256", {})
    _require(
        _tensor_record(embedding_sum[0, -1], "signed_q16.16")["sha256"]
        == block_zero_checkpoints.get("block.input")
        and _tensor_record(block_output_tensors[0][0, -1], "signed_q16.16")["sha256"]
        == block_zero_checkpoints.get("block.output"),
        "block_zero_boundary_mismatch",
        str(step_index),
    )
    _require(
        final_layer_norm["output"]["sha256"]
        == expected_evidence.get("nonlinear_boundary_sha256", {}).get(
            "transformer.ln_f.output"
        ),
        "final_layer_norm_boundary_mismatch",
        str(step_index),
    )

    step = {
        "step_index": step_index,
        "context_tokens": list(context_tokens),
        "context_length": len(context_tokens),
        "context_sha256": context_sha256,
        "input_token_source": (
            "token_contract.initial_context"
            if step_index == 0
            else f"steps.{step_index - 1}.selected_token.hardware_feedback"
        ),
        "feedback_token": None if step_index == 0 else int(context_tokens[-1]),
        "embedding": embedding,
        "transformer_blocks": blocks,
        "final_layer_norm": final_layer_norm,
        "lm_head": lm_head,
        "selected_token": selected,
        "predecessor": {
            "canonical_step_sha256": predecessor_step["step_sha256"],
            "evidence_sha256": predecessor_step["evidence_sha256"],
        },
        "boundary_order": list(BOUNDARY_ORDER),
    }
    return {**step, "step_sha256": canonical_sha256(step)}


def capture_model_token_steps(
    bundle: exact_adapter.ExactModelBundle | None,
    initial_context: Sequence[int],
    *,
    step_count: int = STEP_COUNT,
    predecessor_steps: Sequence[Mapping[str, Any]] | None = None,
    host_second_token: int | None = None,
    host_intermediate_states: object | None = None,
) -> list[dict[str, Any]]:
    """Capture two linked steps while refusing all host intermediate inputs."""

    _require(
        host_second_token is None,
        "host_second_token_forbidden",
        "the second token must come from step 0 greedy selection",
    )
    _require(
        host_intermediate_states is None,
        "host_intermediate_states_forbidden",
        "intermediate model states must be produced by the model invocation",
    )
    _require(step_count == STEP_COUNT, "step_count_mismatch", "exactly two steps required")
    _require(
        list(initial_context) == PROMPT_TOKENS,
        "initial_context_mismatch",
        str(list(initial_context)),
    )
    _require(bundle is not None, "bundle_required", "authenticated exact model required")
    _require(
        predecessor_steps is not None and len(predecessor_steps) >= STEP_COUNT,
        "generation_authority_mismatch",
        "two predecessor steps required",
    )

    context = list(initial_context)
    steps: list[dict[str, Any]] = []
    for step_index in range(step_count):
        step = _capture_step(bundle.model, context, step_index, predecessor_steps[step_index])
        steps.append(step)
        context.append(step["selected_token"]["value"])
    return steps


def validate_model_contract(
    contract_model: Mapping[str, Any], manifest_model: Mapping[str, Any]
) -> None:
    """Combine the immutable input identity with the package's tied-weight flag."""

    contract_expected = {
        key: value for key, value in MODEL_CONTRACT.items() if key != "tie_word_embeddings"
    }
    _require(
        all(contract_model.get(key) == value for key, value in contract_expected.items()),
        "model_contract_mismatch",
        "authenticated input contract differs",
    )
    manifest_expected = {
        "model_type": MODEL_CONTRACT["architecture"],
        **{
            key: MODEL_CONTRACT[key]
            for key in (
                "n_layer",
                "hidden_size",
                "n_head",
                "head_dim",
                "vocab_size",
                "max_context",
                "tie_word_embeddings",
            )
        },
    }
    _require(
        all(manifest_model.get(key) == value for key, value in manifest_expected.items()),
        "model_contract_mismatch",
        "authenticated package manifest differs",
    )


def build_oracle(
    contract_path: Path,
    package_path: Path,
    model_path: Path,
    generation_path: Path,
    *,
    step_count: int = STEP_COUNT,
    host_second_token: int | None = None,
    host_intermediate_states: object | None = None,
) -> dict[str, Any]:
    generation = _load_generation_verifier(Path(generation_path))
    try:
        bundle = exact_adapter.load_successor_exact_model(
            Path(contract_path),
            Path(package_path),
            Path(model_path),
            Path(generation_path),
        )
    except ValueError as error:
        raise ModelTokenStepOracleError("model_authentication_failed", str(error)) from error

    canonical_steps = generation.get("generation", {}).get("canonical_steps")
    _require(
        isinstance(canonical_steps, list) and len(canonical_steps) >= STEP_COUNT,
        "generation_authority_mismatch",
        "predecessor lacks two canonical steps",
    )
    steps = capture_model_token_steps(
        bundle,
        PROMPT_TOKENS,
        step_count=step_count,
        predecessor_steps=canonical_steps,
        host_second_token=host_second_token,
        host_intermediate_states=host_intermediate_states,
    )
    contract = _load_json(Path(contract_path))
    validate_model_contract(contract.get("model", {}), bundle.manifest.get("model", {}))
    token_contract = {
        "initial_context": PROMPT_TOKENS,
        "step_count": STEP_COUNT,
        "host_inputs": ["initial_context"],
        "forbidden_host_inputs": ["second_token", "intermediate_states"],
        "feedback_rule": "append_previous_step_greedy_token_inside_orchestrator",
        "greedy_tie_break": "smallest_token_id_among_equal_maxima",
    }
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "scope": "exact fixed-point model-level two-token oracle; no RTL or board claim",
        "identity": {
            "contract_file_sha256": _sha256(Path(contract_path)),
            "generation_file_sha256": _sha256(Path(generation_path)),
            "generation_artifact_sha256": generation["artifact_sha256"],
            "package_manifest_sha256": _sha256(Path(package_path) / "manifest.json"),
            "package_weights_sha256": _sha256(Path(package_path) / "weights.bin"),
            "package_scales_sha256": _sha256(Path(package_path) / "scales.bin"),
            "model_config_sha256": _sha256(Path(model_path) / "config.json"),
            "exact_adapter_sha256": _sha256(Path(exact_adapter.__file__)),
            "capture_source_sha256": _sha256(Path(__file__)),
        },
        "model_contract": MODEL_CONTRACT,
        "arithmetic_contract": ARITHMETIC_CONTRACT,
        "token_contract": token_contract,
        "boundary_contract": _boundary_contract(),
        "steps": steps,
        "claims": {
            "authenticated_package_used": True,
            "datapath_semantics_modified": False,
            "two_tokens_selected_greedily": True,
            "second_token_is_first_token_feedback": True,
            "host_intermediate_inputs_accepted": False,
            "rtl_generated": False,
            "board_executed": False,
        },
    }
    result["artifact_sha256"] = canonical_sha256(result)
    verify_oracle_fixture(result)
    return result


def _require_keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    _require(set(value) == expected, code, f"expected {sorted(expected)}, got {sorted(value)}")


def _validate_tensor_record(value: Mapping[str, Any], label: str) -> None:
    _require_keys(
        value,
        {"shape", "dtype", "sha256", "little_endian_int64_sha256", "bytes"},
        "tensor_record_schema_mismatch",
    )
    _require(
        isinstance(value["shape"], list)
        and all(isinstance(item, int) and item > 0 for item in value["shape"]),
        "tensor_record_schema_mismatch",
        label,
    )
    _require(
        isinstance(value["dtype"], str)
        and SHA256.fullmatch(value["sha256"]) is not None
        and SHA256.fullmatch(value["little_endian_int64_sha256"]) is not None
        and value["bytes"] == 8 * _shape_elements(value["shape"]),
        "tensor_record_schema_mismatch",
        label,
    )


def _validate_buffer_record(value: Mapping[str, Any], label: str) -> None:
    _require_keys(
        value,
        {"shape", "dtype", "little_endian_int64_sha256", "bytes", "record_sha256"},
        "buffer_record_schema_mismatch",
    )
    payload = {key: item for key, item in value.items() if key != "record_sha256"}
    _require(
        isinstance(value["shape"], list)
        and all(isinstance(item, int) and item > 0 for item in value["shape"])
        and isinstance(value["dtype"], str)
        and SHA256.fullmatch(value["little_endian_int64_sha256"]) is not None
        and value["bytes"] == 8 * _shape_elements(value["shape"])
        and value["record_sha256"] == canonical_sha256(payload),
        "buffer_record_schema_mismatch",
        label,
    )


def _shape_elements(shape: Sequence[int]) -> int:
    result = 1
    for dimension in shape:
        result *= dimension
    return result


def verify_oracle_fixture(
    fixture: Mapping[str, Any], *, live_oracle: Mapping[str, Any] | None = None
) -> None:
    """Validate schema, predecessor projection, self-hashes, and optional replay."""

    _require_keys(fixture, FIXTURE_KEYS, "fixture_schema_mismatch")
    _require(
        fixture.get("schema") == SCHEMA and fixture.get("status") == STATUS,
        "fixture_identity_mismatch",
        "schema/status",
    )
    _require(
        fixture.get("artifact_sha256")
        == canonical_sha256(
            {key: value for key, value in fixture.items() if key != "artifact_sha256"}
        ),
        "fixture_self_hash_mismatch",
        "artifact_sha256",
    )
    identity = fixture.get("identity")
    _require(isinstance(identity, Mapping), "provenance_identity_mismatch", "identity object")
    _require_keys(identity, IDENTITY_KEYS, "provenance_identity_mismatch")
    contract = _load_json(DEFAULT_CONTRACT)
    _require(
        identity["contract_file_sha256"] == _sha256(DEFAULT_CONTRACT)
        and identity["exact_adapter_sha256"] == _sha256(Path(exact_adapter.__file__))
        and identity["capture_source_sha256"] == _sha256(Path(__file__))
        and identity["package_manifest_sha256"] == contract["package"]["manifest_sha256"]
        and identity["package_weights_sha256"] == contract["package"]["sha256"]
        and identity["package_scales_sha256"]
        == contract["package"]["files"]["scales.bin"]["sha256"]
        and identity["model_config_sha256"] == EXPECTED_MODEL_CONFIG_SHA256
        and all(SHA256.fullmatch(str(value)) is not None for value in identity.values()),
        "provenance_identity_mismatch",
        "authenticated component identity differs",
    )
    if DEFAULT_MODEL.is_dir():
        _require(
            _sha256(DEFAULT_MODEL / "config.json") == EXPECTED_MODEL_CONFIG_SHA256,
            "provenance_identity_mismatch",
            "model config identity differs",
        )
    token_contract = fixture.get("token_contract")
    _require(isinstance(token_contract, Mapping), "token_contract_schema_mismatch", "object")
    _require_keys(token_contract, TOKEN_CONTRACT_KEYS, "token_contract_schema_mismatch")
    _require(
        token_contract == {
            "initial_context": PROMPT_TOKENS,
            "step_count": STEP_COUNT,
            "host_inputs": ["initial_context"],
            "forbidden_host_inputs": ["second_token", "intermediate_states"],
            "feedback_rule": "append_previous_step_greedy_token_inside_orchestrator",
            "greedy_tie_break": "smallest_token_id_among_equal_maxima",
        },
        "token_contract_mismatch",
        "exact token feedback contract differs",
    )
    _require(fixture.get("model_contract") == MODEL_CONTRACT, "model_contract_mismatch", "model")
    _require(
        fixture.get("arithmetic_contract") == ARITHMETIC_CONTRACT,
        "arithmetic_contract_mismatch",
        "arithmetic",
    )
    boundary_contract = fixture.get("boundary_contract")
    _require(
        isinstance(boundary_contract, Mapping)
        and boundary_contract == _boundary_contract(),
        "boundary_contract_mismatch",
        "exact ordered model boundary contract differs",
    )

    generation = _load_json(DEFAULT_GENERATION)
    _require(
        generation.get("artifact_sha256")
        == fixture.get("identity", {}).get("generation_artifact_sha256")
        and _sha256(DEFAULT_GENERATION)
        == fixture.get("identity", {}).get("generation_file_sha256"),
        "generation_authority_mismatch",
        "generation identity",
    )
    canonical_steps = generation.get("generation", {}).get("canonical_steps", [])
    steps = fixture.get("steps")
    _require(
        isinstance(steps, list) and len(steps) == STEP_COUNT,
        "step_schema_mismatch",
        "exactly two steps required",
    )
    contexts = [PROMPT_TOKENS, PROMPT_TOKENS + [canonical_steps[0]["selected_token"]]]
    for index, step in enumerate(steps):
        _require(isinstance(step, Mapping), "step_schema_mismatch", str(index))
        _require_keys(step, STEP_KEYS, "step_schema_mismatch")
        _require(
            step["step_sha256"]
            == canonical_sha256(
                {key: value for key, value in step.items() if key != "step_sha256"}
            ),
            "step_self_hash_mismatch",
            str(index),
        )
        _require(
            step["step_index"] == index
            and step["context_tokens"] == contexts[index]
            and step["context_length"] == len(contexts[index])
            and step["context_sha256"] == canonical_steps[index]["context_sha256"],
            "step_context_mismatch",
            str(index),
        )
        expected_source = (
            "token_contract.initial_context"
            if index == 0
            else "steps.0.selected_token.hardware_feedback"
        )
        _require(
            step["input_token_source"] == expected_source
            and step["feedback_token"]
            == (None if index == 0 else canonical_steps[0]["selected_token"]),
            "token_feedback_mismatch",
            str(index),
        )
        embedding = step["embedding"]
        _require_keys(
            embedding,
            {"token", "position", "sum", "boundary_sha256"},
            "embedding_schema_mismatch",
        )
        _require(
            embedding["boundary_sha256"]
            == canonical_sha256(
                {key: value for key, value in embedding.items() if key != "boundary_sha256"}
            ),
            "embedding_self_hash_mismatch",
            str(index),
        )
        for name in ("token", "position", "sum"):
            _validate_tensor_record(embedding[name], f"step {index} embedding {name}")
        blocks = step["transformer_blocks"]
        _require(
            isinstance(blocks, list)
            and [block.get("block_index") for block in blocks] == list(range(8)),
            "transformer_block_schema_mismatch",
            str(index),
        )
        for block_index, block in enumerate(blocks):
            _require_keys(
                block,
                {"block_index", "input", "output", "boundary_sha256"},
                "transformer_block_schema_mismatch",
            )
            _require(
                block["boundary_sha256"]
                == canonical_sha256(
                    {key: value for key, value in block.items() if key != "boundary_sha256"}
                ),
                "transformer_block_self_hash_mismatch",
                f"{index}:{block_index}",
            )
            _validate_tensor_record(block["input"], f"step {index} block {block_index} input")
            _validate_tensor_record(block["output"], f"step {index} block {block_index} output")
        _require(
            embedding["sum"] == blocks[0]["input"]
            and all(
                blocks[block_index]["output"] == blocks[block_index + 1]["input"]
                for block_index in range(7)
            ),
            "model_boundary_disconnected",
            f"step {index}: embedding/transformer chain",
        )
        final_layer_norm = step["final_layer_norm"]
        _require_keys(
            final_layer_norm,
            {"input", "output", "boundary_sha256"},
            "final_layer_norm_schema_mismatch",
        )
        _require(
            final_layer_norm["boundary_sha256"]
            == canonical_sha256(
                {
                    key: value
                    for key, value in final_layer_norm.items()
                    if key != "boundary_sha256"
                }
            ),
            "final_layer_norm_self_hash_mismatch",
            str(index),
        )
        _validate_tensor_record(final_layer_norm["input"], f"step {index} final LayerNorm input")
        _validate_tensor_record(final_layer_norm["output"], f"step {index} final LayerNorm output")
        _require(
            blocks[-1]["output"] == final_layer_norm["input"],
            "model_boundary_disconnected",
            f"step {index}: block 7/final LayerNorm",
        )
        lm_head = step["lm_head"]
        _require_keys(
            lm_head,
            {
                "input",
                "last_logits",
                "full_context_logits",
                "weight_source",
                "weight_codes",
                "weight_scales",
                "boundary_sha256",
            },
            "lm_head_schema_mismatch",
        )
        _require(
            lm_head["boundary_sha256"]
            == canonical_sha256(
                {key: value for key, value in lm_head.items() if key != "boundary_sha256"}
            )
            and lm_head["weight_source"] == "token_embedding.weight",
            "lm_head_boundary_mismatch",
            str(index),
        )
        for name in ("input", "last_logits", "full_context_logits"):
            _validate_tensor_record(lm_head[name], f"step {index} lm_head {name}")
        _validate_buffer_record(lm_head["weight_codes"], f"step {index} lm_head weight codes")
        _validate_buffer_record(lm_head["weight_scales"], f"step {index} lm_head weight scales")
        _require(
            final_layer_norm["output"] == lm_head["input"],
            "model_boundary_disconnected",
            f"step {index}: final LayerNorm/LM head",
        )
        predecessor = canonical_steps[index]
        _require(
            lm_head["last_logits"]["sha256"]
            == predecessor["evidence"]["logits_sha256"]
            and lm_head["full_context_logits"]["sha256"]
            == predecessor["evidence"]["full_context_logits_sha256"],
            "predecessor_logits_mismatch",
            str(index),
        )
        selected = step["selected_token"]
        _require(
            selected.get("value") == predecessor["selected_token"]
            and selected.get("tie_count") == predecessor["tie_count"]
            and selected.get("sha256")
            == canonical_sha256({"dtype": "token_id", "value": selected.get("value")}),
            "predecessor_token_mismatch",
            str(index),
        )
        _require(
            step["predecessor"]
            == {
                "canonical_step_sha256": predecessor["step_sha256"],
                "evidence_sha256": predecessor["evidence_sha256"],
            },
            "generation_authority_mismatch",
            str(index),
        )
        _require(
            step["boundary_order"] == boundary_contract["order"],
            "boundary_order_mismatch",
            str(index),
        )
        _require(
            step["step_sha256"] == EXPECTED_STEP_SHA256[index],
            "semantic_authority_mismatch",
            f"step {index} differs from the pinned exact-model replay",
        )
    _require(
        steps[1]["feedback_token"] == steps[0]["selected_token"]["value"],
        "token_feedback_mismatch",
        "step 1 must consume step 0 token",
    )
    _require(
        fixture.get("claims")
        == {
            "authenticated_package_used": True,
            "datapath_semantics_modified": False,
            "two_tokens_selected_greedily": True,
            "second_token_is_first_token_feedback": True,
            "host_intermediate_inputs_accepted": False,
            "rtl_generated": False,
            "board_executed": False,
        },
        "claims_mismatch",
        "scope claims differ",
    )
    if live_oracle is not None:
        _require(
            fixture == live_oracle,
            "live_replay_mismatch",
            "committed fixture differs from authenticated replay",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--generation", type=Path, default=DEFAULT_GENERATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    result = build_oracle(
        args.contract,
        args.package,
        args.model_path,
        args.generation,
        step_count=STEP_COUNT,
    )
    if args.verify and args.output.is_file():
        verify_oracle_fixture(_load_json(args.output), live_oracle=result)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        verify_oracle_fixture(_load_json(args.output), live_oracle=result)
    print(json.dumps({
        "status": result["status"],
        "tokens": [step["selected_token"]["value"] for step in result["steps"]],
        "artifact_sha256": result["artifact_sha256"],
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
