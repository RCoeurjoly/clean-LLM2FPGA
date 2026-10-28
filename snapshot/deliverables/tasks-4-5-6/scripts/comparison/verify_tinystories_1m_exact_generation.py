#!/usr/bin/env python3
"""Prove exact eager/exported greedy generation for the frozen TinyStories-1M input."""

from __future__ import annotations

import argparse
import copy
import gc
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import torch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from TinyStories.model_adapter_exact_package import (
    CHECKPOINT_SHAPES,
    GEMV_NAMES,
    NONLINEAR_BOUNDARY_NAMES,
    QDQ_BOUNDARY_NAMES,
    ExactModelBundle,
    _TraceOutputs,
    _named_tensor_state_sha256,
    canonical_sha256,
    exported_program_identity,
    load_exact_model,
)


SCHEMA = "tinystories-1m-exact-generation-v2"
PROMPT_IDS = [7454, 2402, 257, 640]
FROZEN_TOKENS = [
    11, 612, 373, 257, 1310, 2576, 3706, 20037,
    13, 1375, 6151, 284, 711, 2354, 287, 262,
]
TASK_1_COMMIT = "c8eb2011ee09c368accdf8efd67c50dfc5564679"
TASK_2_COMMIT = "a2eda783819bbfb35a833d1a45d3dd125a176973"
TASK_1_CONTRACT_SHA256 = "859fe3095a4842e413ee99466f5dc63d5420d0e890a3dce0cf7a52e3bd2d1d3c"
TASK_1_AUDIT_FILE_SHA256 = "3cf8a5b9db8acf0ca04e92277c0f9f07c81900a4c754626183bd1d22063616bd"
TASK_2_ARTIFACT_FILE_SHA256 = "173f54586fd37f06e03e9b754568df729591d2cacc5b4a238407ea553d3d529a"
TASK_2_ARTIFACT_SHA256 = "af1901917b52876a9b3343712b89928b272e5dd237cd491ddd9d462c56a52838"
TASK_2_MODEL_RECEIPT_SHA256 = "5e56907e60c83c5d98b3c3fe88772b7dfba71e53a9435de548a9d54ea7497834"
MANIFEST_CANONICAL_SHA256 = "24e8c2b2604a00b9adffbf947770bbd395e6129f50482812fe15ba9dac181244"
MODEL_STATE_TENSORS_SHA256 = "1f56e70699f45f702b6a765960b60246f13a5a8d74f53d318b1a1e9839f66403"
AUTHENTICATED_RESULT_SHA256 = "c18106f25030ec58dfd3abc5d75d774506aca65b655fc34b284076b1294f8644"
TASK_2_ARTIFACT_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-package-model.json")
CONTRACT_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-input-contract.json")
AUDIT_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-input-audit.json")
PROFILE_RELATIVE = Path("artifacts/reference/tinystories-1m-fixed-hardware-qdq-profile.json")
CERTIFICATE_RELATIVE = Path("artifacts/reference/tinystories-1m-exact-reachable-domain.json")
ORACLE_RELATIVE = Path("artifacts/reference/tinystories-1m-fixed-logits-oracle.json")
VERIFIER_RELATIVE = Path("scripts/comparison/verify_tinystories_1m_exact_generation.py")
TEST_RELATIVE = Path("tests/test_tinystories_1m_exact_generation.py")


class ExactGenerationError(ValueError):
    """Generation evidence is incomplete, mismatched, or unauthenticated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ExactGenerationError(code, message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ExactGenerationError("invalid_json", f"{path}: {error}") from error
    _require(isinstance(value, dict), "invalid_json", f"{path} must contain an object")
    return value


def _task_2_artifact_sha256(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "artifact_sha256"})


def _canonical_state_binding(
    values: Mapping[str, torch.Tensor], wrapper_prefix: str | None = None
) -> dict[str, Any]:
    """Bind exact tensor names/dtypes/shapes/bytes after one proven wrapper normalization."""

    normalized: dict[str, torch.Tensor] = {}
    for original_name, tensor in values.items():
        _require(isinstance(original_name, str) and isinstance(tensor, torch.Tensor),
                 "export_state_schema_mismatch", str(original_name))
        if wrapper_prefix is None:
            name = original_name
        else:
            _require(original_name.startswith(wrapper_prefix), "export_state_name_mismatch",
                     original_name)
            name = original_name[len(wrapper_prefix):]
            _require(bool(name), "export_state_name_mismatch", original_name)
        _require(name not in normalized, "export_state_name_collision", name)
        normalized[name] = tensor
    tensors = []
    for name, tensor in sorted(normalized.items()):
        value = tensor.detach().cpu().contiguous()
        tensors.append({
            "name": name,
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "bytes_sha256": hashlib.sha256(value.numpy().tobytes()).hexdigest(),
        })
    normalization = "none" if wrapper_prefix is None else f"removed_exact_prefix:{wrapper_prefix}"
    canonical_payload = {"normalization": normalization, "tensors": tensors}
    return {
        **canonical_payload,
        "tensor_count": len(tensors),
        "canonical_sha256": canonical_sha256(canonical_payload),
        "normalized_tensors_sha256": canonical_sha256(tensors),
        "task_2_named_state_sha256": _named_tensor_state_sha256(normalized),
    }


def _require_normalized_state_match(
    actual: Mapping[str, Any], authority: Mapping[str, Any], label: str
) -> None:
    """Require an internally sound normalized binding equal to authenticated model state."""

    for value in (actual, authority):
        tensors = value.get("tensors")
        _require(isinstance(tensors, list), "export_state_binding_mismatch", label)
        _require(value.get("tensor_count") == len(tensors)
                 and value.get("normalized_tensors_sha256") == canonical_sha256(tensors)
                 and value.get("canonical_sha256") == canonical_sha256({
                     "normalization": value.get("normalization"), "tensors": tensors,
                 }), "export_state_binding_mismatch", label)
    for key in (
        "tensors", "tensor_count", "normalized_tensors_sha256", "task_2_named_state_sha256",
    ):
        _require(actual.get(key) == authority.get(key),
                 "export_state_binding_mismatch", f"{label}: {key}")


def _mode_evidence_summary(
    mode: str, step_evidence_sha256: Sequence[str], tensors_per_step: int
) -> dict[str, Any]:
    payload = {
        "mode": mode,
        "step_evidence_sha256": list(step_evidence_sha256),
        "step_count": len(step_evidence_sha256),
        "tensors_per_step": tensors_per_step,
        "tensor_observation_count": len(step_evidence_sha256) * tensors_per_step,
    }
    return {**payload, "aggregate_sha256": canonical_sha256(payload)}


def _authenticate_authority(bundle: ExactModelBundle, repo_root: Path) -> dict[str, Any]:
    """Authenticate the exact Task 1/2 boundary before any candidate execution."""

    repo_root = Path(repo_root)
    contract_path = repo_root / CONTRACT_RELATIVE
    audit_path = repo_root / AUDIT_RELATIVE
    profile_path = repo_root / PROFILE_RELATIVE
    certificate_path = repo_root / CERTIFICATE_RELATIVE
    oracle_path = repo_root / ORACLE_RELATIVE
    task_2_path = repo_root / TASK_2_ARTIFACT_RELATIVE
    _require(_sha256(contract_path) == TASK_1_CONTRACT_SHA256,
             "task_1_identity_mismatch", "contract file drifted")
    _require(_sha256(audit_path) == TASK_1_AUDIT_FILE_SHA256,
             "task_1_identity_mismatch", "audit file drifted")
    _require(_sha256(task_2_path) == TASK_2_ARTIFACT_FILE_SHA256,
             "task_2_identity_mismatch", "exact-model artifact file drifted")

    task_2 = _load_json(task_2_path)
    _require(task_2.get("artifact_sha256") == TASK_2_ARTIFACT_SHA256
             and _task_2_artifact_sha256(task_2) == TASK_2_ARTIFACT_SHA256,
             "task_2_identity_mismatch", "exact-model artifact self hash drifted")
    source_files = task_2.get("identity", {}).get("source_files", {})
    _require(isinstance(source_files, dict) and source_files,
             "task_2_identity_mismatch", "exact-model source closure is missing")
    for relative, expected in source_files.items():
        path = repo_root / relative
        _require(path.is_file() and _sha256(path) == expected,
                 "task_2_identity_mismatch", f"source drift: {relative}")

    contract = _load_json(contract_path)
    audit = _load_json(audit_path)
    profile = _load_json(profile_path)
    certificate = _load_json(certificate_path)
    oracle = _load_json(oracle_path)
    task_2_identity = task_2.get("identity", {})
    _require(audit.get("sha256") == canonical_sha256({
        key: value for key, value in audit.items() if key != "sha256"
    }), "task_1_identity_mismatch", "audit self hash drifted")
    _require(profile.get("profile_sha256") == canonical_sha256({
        key: value for key, value in profile.items() if key != "profile_sha256"
    }), "task_2_identity_mismatch", "fixed profile self hash drifted")
    _require(certificate.get("certificate_sha256") == canonical_sha256({
        key: value for key, value in certificate.items() if key != "certificate_sha256"
    }), "task_2_identity_mismatch", "reachable certificate self hash drifted")
    _require(oracle.get("oracle_sha256") == canonical_sha256({
        key: value for key, value in oracle.items() if key != "oracle_sha256"
    }), "task_2_identity_mismatch", "independent oracle self hash drifted")
    _require(_sha256(profile_path) == task_2_identity.get("fixed_profile", {}).get("sha256"),
             "task_2_identity_mismatch", "fixed profile file drifted")
    _require(_sha256(certificate_path)
             == task_2_identity.get("reachable_certificate", {}).get("file_sha256")
             and certificate["certificate_sha256"]
             == task_2_identity.get("reachable_certificate", {}).get("certificate_sha256"),
             "task_2_identity_mismatch", "reachable certificate identity drifted")
    _require(_sha256(oracle_path)
             == task_2_identity.get("fixed_logits_oracle", {}).get("file_sha256")
             and oracle["oracle_sha256"]
             == task_2_identity.get("fixed_logits_oracle", {}).get("oracle_sha256"),
             "task_2_identity_mismatch", "independent oracle identity drifted")
    package_origin = Path(str(contract.get("package", {}).get("origin", "")))
    manifest_path = package_origin / "manifest.json"
    manifest_identity = task_2_identity.get("package", {}).get("files", {}).get("manifest.json", {})
    _require(manifest_path.is_file() and manifest_path.stat().st_size == manifest_identity.get("size")
             and _sha256(manifest_path) == manifest_identity.get("sha256"),
             "task_2_identity_mismatch", "package manifest file drifted")
    manifest = _load_json(manifest_path)

    for name, live, expected in (
        ("contract", bundle.contract, contract),
        ("audit", bundle.audit, audit),
        ("profile", bundle.profile, profile),
        ("certificate", bundle.certificate, certificate),
        ("manifest", bundle.manifest, manifest),
        ("oracle", bundle.oracle, oracle),
    ):
        _require(live == expected, f"live_{name}_mismatch",
                 f"live {name} differs from authenticated disk object")

    oracle_values = oracle.get("logits", {}).get("values")
    _require(isinstance(oracle_values, list), "task_2_identity_mismatch", "oracle logits missing")
    disk_oracle_logits = torch.tensor(oracle_values, dtype=torch.int64)
    _require(bundle.oracle_logits.dtype == torch.int64
             and tuple(bundle.oracle_logits.shape) == tuple(disk_oracle_logits.shape)
             and torch.equal(bundle.oracle_logits.detach().cpu(), disk_oracle_logits),
             "live_oracle_logits_mismatch", "live oracle tensor differs from authenticated disk vector")

    receipt = bundle.receipt
    receipt_payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    _require(receipt.get("status") == "authenticated_fixed_point_model"
             and receipt.get("receipt_sha256") == canonical_sha256(receipt_payload)
             and receipt.get("receipt_sha256") == TASK_2_MODEL_RECEIPT_SHA256
             and task_2.get("identity", {}).get("model_receipt_sha256") == TASK_2_MODEL_RECEIPT_SHA256,
             "task_2_receipt_mismatch", "exact model receipt is not the accepted Task 2 receipt")
    independent = receipt.get("independent_logits_oracle", {})
    _require(independent.get("status") == "full_logits_bit_exact"
             and independent.get("canonical_sha256") == oracle.get("logits", {}).get("canonical_sha256"),
             "task_2_oracle_mismatch", "independent fixed-logits oracle is not authenticated")
    authenticated_model_state = task_2.get("exported_program", {}).get("state_dict_sha256")
    model_state_binding = _canonical_state_binding(bundle.model.state_dict())
    actual_model_state = model_state_binding["task_2_named_state_sha256"]
    _require(actual_model_state == authenticated_model_state,
             "task_2_model_state_mismatch", "live exact-model state differs from Task 2 export")
    _require(contract.get("reference", {}).get("prompt_tokens") == PROMPT_IDS
             and contract.get("reference", {}).get("tokens") == FROZEN_TOKENS,
             "task_1_identity_mismatch", "bundle reference fixture drifted")

    def component(value: Any, **extra: Any) -> dict[str, Any]:
        return {
            "status": "matched_authenticated_authority",
            "canonical_sha256": canonical_sha256(value),
            **extra,
        }

    live_components = {
        "contract": component(contract, file_sha256=TASK_1_CONTRACT_SHA256),
        "audit": component(audit, file_sha256=TASK_1_AUDIT_FILE_SHA256,
                           self_sha256=audit["sha256"]),
        "fixed_profile": component(profile, file_sha256=_sha256(profile_path),
                                   self_sha256=profile["profile_sha256"]),
        "reachable_certificate": component(
            certificate, file_sha256=_sha256(certificate_path),
            self_sha256=certificate["certificate_sha256"],
        ),
        "manifest": component(manifest, file_sha256=_sha256(manifest_path)),
        "independent_oracle": component(
            oracle, file_sha256=_sha256(oracle_path), self_sha256=oracle["oracle_sha256"],
        ),
        "oracle_logits": component(oracle_values,
                                   little_endian_int64_sha256=_little_endian_int64_sha256(
                                       disk_oracle_logits
                                   )),
        "receipt": component(receipt_payload, self_sha256=receipt["receipt_sha256"]),
        "model_state": component(model_state_binding, state_binding=model_state_binding),
    }

    return {
        "task_1": {
            "commit": TASK_1_COMMIT,
            "contract_file_sha256": TASK_1_CONTRACT_SHA256,
            "audit_file_sha256": TASK_1_AUDIT_FILE_SHA256,
            "audit_payload_sha256": audit["sha256"],
        },
        "task_2": {
            "commit": TASK_2_COMMIT,
            "artifact_file_sha256": TASK_2_ARTIFACT_FILE_SHA256,
            "artifact_sha256": TASK_2_ARTIFACT_SHA256,
            "model_receipt_sha256": TASK_2_MODEL_RECEIPT_SHA256,
            "fixed_logits_oracle_sha256": oracle["oracle_sha256"],
            "fixed_logits_vector_sha256": independent["canonical_sha256"],
            "model_state_sha256": actual_model_state,
        },
        "live_components": live_components,
    }


def _tensor_sha256(tensor: torch.Tensor, semantic_dtype: str) -> str:
    value = tensor.detach().cpu().to(torch.int64).contiguous()
    payload = {
        "shape": list(value.shape),
        "dtype": semantic_dtype,
        "values": value.tolist(),
    }
    return canonical_sha256(payload)


def _little_endian_int64_sha256(tensor: torch.Tensor) -> str:
    value = tensor.detach().cpu().to(torch.int64).contiguous()
    return hashlib.sha256(value.numpy().astype("<i8", copy=False).tobytes()).hexdigest()


def _execution_observations(outputs: Sequence[torch.Tensor]) -> dict[str, Any]:
    checkpoint_end = 1 + len(CHECKPOINT_SHAPES)
    qdq_end = checkpoint_end + len(QDQ_BOUNDARY_NAMES) * 3
    accumulator_end = qdq_end + len(GEMV_NAMES)
    nonlinear_end = accumulator_end + len(NONLINEAR_BOUNDARY_NAMES)
    _require(len(outputs) == nonlinear_end, "trace_schema_mismatch", str(len(outputs)))

    logits = outputs[0]
    last_logits = logits[0, -1]
    checkpoint_hashes = {
        name: _tensor_sha256(tensor, "signed_q16.16")
        for name, tensor in zip(CHECKPOINT_SHAPES, outputs[1:checkpoint_end], strict=True)
    }
    qdq_hashes: dict[str, dict[str, str]] = {}
    qdq = outputs[checkpoint_end:qdq_end]
    for index, name in enumerate(QDQ_BOUNDARY_NAMES):
        tensors = qdq[index * 3:index * 3 + 3]
        qdq_hashes[name] = {
            semantic: _tensor_sha256(tensor, dtype)
            for semantic, tensor, dtype in zip(
                ("codes", "scales", "dequantized"),
                tensors,
                ("signed_int8_codes", "unsigned_q8.24", "signed_q16.16"),
                strict=True,
            )
        }
    accumulator_hashes = {
        name: _tensor_sha256(tensor, "signed_int64_serial_accumulator")
        for name, tensor in zip(GEMV_NAMES, outputs[qdq_end:accumulator_end], strict=True)
    }
    nonlinear_hashes = {
        name: _tensor_sha256(tensor, "signed_q16.16")
        for name, tensor in zip(
            NONLINEAR_BOUNDARY_NAMES, outputs[accumulator_end:nonlinear_end], strict=True
        )
    }
    observation_groups = {
        "checkpoint_sha256": checkpoint_hashes,
        "qdq_boundary_sha256": qdq_hashes,
        "gemv_accumulator_sha256": accumulator_hashes,
        "nonlinear_boundary_sha256": nonlinear_hashes,
    }
    return {
        "logits_shape": list(last_logits.shape),
        "logits_dtype": "signed_fixed_point_int64",
        "logits_sha256": _tensor_sha256(last_logits, "signed_fixed_point_logits"),
        "logits_little_endian_int64_sha256": _little_endian_int64_sha256(last_logits),
        "full_context_logits_sha256": _tensor_sha256(logits, "signed_fixed_point_logits"),
        **observation_groups,
        "observation_sha256": canonical_sha256(observation_groups),
    }


def select_greedy_token(logits: Sequence[int] | torch.Tensor) -> tuple[int, int]:
    """Select top-1, explicitly resolving equal maxima to the smallest token ID."""

    values = torch.as_tensor(logits, dtype=torch.int64).flatten()
    _require(values.numel() > 0, "empty_logits", "at least one logit is required")
    maximum = torch.max(values)
    tied_ids = torch.nonzero(values == maximum, as_tuple=False).flatten()
    return int(torch.min(tied_ids)), int(tied_ids.numel())


class _StaticExportCache:
    """One honest static trace export per input length, replayable for any token values."""

    def __init__(self, bundle: ExactModelBundle, authority_state: Mapping[str, Any]) -> None:
        self._bundle = bundle
        self._authority_state = authority_state
        self._active_length: int | None = None
        self._active_program: torch.export.ExportedProgram | None = None
        self._identities: dict[int, dict[str, Any]] = {}
        self.replay_count = 0

    def export_length(self, length: int) -> None:
        _require(length not in self._identities, "duplicate_static_export", str(length))
        _require(self._active_program is None, "static_export_not_released", str(self._active_length))
        example = torch.zeros((1, length), dtype=torch.int64)
        self._active_program = torch.export.export(
            _TraceOutputs(self._bundle.model).eval(), (example,), strict=False
        )
        self._active_length = length
        state_binding = _canonical_state_binding(
            self._active_program.state_dict, wrapper_prefix="model."
        )
        _require_normalized_state_match(
            state_binding, self._authority_state, f"static export context {length}"
        )
        identity = exported_program_identity(self._active_program)
        identity["normalized_state_binding"] = {
            "normalization": state_binding["normalization"],
            "tensor_count": state_binding["tensor_count"],
            "canonical_sha256": state_binding["canonical_sha256"],
            "normalized_tensors_sha256": state_binding["normalized_tensors_sha256"],
            "task_2_named_state_sha256": state_binding["task_2_named_state_sha256"],
            "authority_ref": "identity.live_components.model_state.state_binding",
        }
        self._identities[length] = identity

    def replay(self, context: torch.Tensor) -> tuple[torch.Tensor, ...]:
        length = int(context.shape[1])
        _require(self._active_program is not None and self._active_length == length,
                 "static_export_length_mismatch", str(length))
        with torch.no_grad():
            outputs = self._active_program.module()(context)
        self.replay_count += 1
        return tuple(outputs)

    def release(self) -> None:
        self._active_program = None
        self._active_length = None
        gc.collect()

    def program_identity(self, length: int) -> dict[str, Any]:
        return self._identities[length]

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": "one_static_trace_export_per_context_length",
            "context_lengths": sorted(self._identities),
            "export_count": len(self._identities),
            "replay_count": self.replay_count,
            "programs": {str(length): identity for length, identity in sorted(self._identities.items())},
            "cache_sha256": canonical_sha256({
                str(length): identity for length, identity in sorted(self._identities.items())
            }),
        }


def _fresh_bundle(bundle: ExactModelBundle) -> ExactModelBundle:
    """Materialize a new bundle and model instance from authenticated in-memory state."""

    return ExactModelBundle(
        contract=copy.deepcopy(bundle.contract),
        audit=copy.deepcopy(bundle.audit),
        profile=copy.deepcopy(bundle.profile),
        certificate=copy.deepcopy(bundle.certificate),
        oracle=copy.deepcopy(bundle.oracle),
        oracle_logits=bundle.oracle_logits.detach().clone(),
        manifest=copy.deepcopy(bundle.manifest),
        model=copy.deepcopy(bundle.model),
        receipt=copy.deepcopy(bundle.receipt),
    )


def _first_mapping_mismatch(
    expected: Mapping[str, Any], actual: Mapping[str, Any], prefix: list[str] | None = None
) -> list[str] | None:
    prefix = [] if prefix is None else prefix
    for key in expected:
        path = prefix + [key]
        if key not in actual:
            return path
        expected_value = expected[key]
        actual_value = actual[key]
        if isinstance(expected_value, Mapping) and isinstance(actual_value, Mapping):
            nested = _first_mapping_mismatch(expected_value, actual_value, path)
            if nested is not None:
                return nested
        elif expected_value != actual_value:
            return path
    for key in actual:
        if key not in expected:
            return prefix + [key]
    return None


def _mapping_path_value(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _completed_run_count(
    runs: Sequence[Mapping[str, Any]], expected_tokens: Sequence[int], required_steps: int
) -> int:
    return sum(
        run.get("tokens") == list(expected_tokens)
        and isinstance(run.get("steps"), list)
        and len(run["steps"]) == required_steps
        and all(step.get("comparison_status") == "matched" for step in run["steps"])
        for run in runs
    )


def _trace_tensor_names() -> list[str]:
    names = ["logits"]
    names.extend(f"checkpoint:{name}" for name in CHECKPOINT_SHAPES)
    names.extend(
        f"qdq:{name}:{semantic}"
        for name in QDQ_BOUNDARY_NAMES
        for semantic in ("codes", "scales", "dequantized")
    )
    names.extend(f"gemv:{name}" for name in GEMV_NAMES)
    names.extend(f"nonlinear:{name}" for name in NONLINEAR_BOUNDARY_NAMES)
    return names


TRACE_TENSOR_NAMES = _trace_tensor_names()
TRACE_TENSORS_PER_STEP = len(TRACE_TENSOR_NAMES)


def _first_trace_tensor_mismatch(
    eager: Sequence[torch.Tensor], exported: Sequence[torch.Tensor]
) -> dict[str, Any] | None:
    if len(eager) != len(exported) or len(eager) != TRACE_TENSORS_PER_STEP:
        return {
            "code": "trace_tensor_count_mismatch",
            "expected_count": TRACE_TENSORS_PER_STEP,
            "eager_count": len(eager),
            "exported_count": len(exported),
        }
    for index, (name, eager_tensor, exported_tensor) in enumerate(
        zip(TRACE_TENSOR_NAMES, eager, exported, strict=True)
    ):
        if (eager_tensor.dtype != exported_tensor.dtype
                or tuple(eager_tensor.shape) != tuple(exported_tensor.shape)
                or not torch.equal(eager_tensor, exported_tensor)):
            return {
                "code": "eager_export_tensor_mismatch",
                "tensor_index": index,
                "tensor_name": name,
                "eager_dtype": str(eager_tensor.dtype),
                "exported_dtype": str(exported_tensor.dtype),
                "eager_shape": list(eager_tensor.shape),
                "exported_shape": list(exported_tensor.shape),
            }
    return None


def verify_generation(
    bundle: ExactModelBundle,
    prompt_ids: Sequence[int],
    expected_tokens: Sequence[int],
    count: int = 3,
) -> dict[str, object]:
    """Run three fresh exact models and compare all 16 eager/exported steps."""

    _require(isinstance(bundle, ExactModelBundle), "bundle_type_mismatch", "ExactModelBundle required")
    _require(list(prompt_ids) == PROMPT_IDS, "prompt_identity_mismatch", str(list(prompt_ids)))
    _require(list(expected_tokens) == FROZEN_TOKENS,
             "expected_tokens_identity_mismatch", str(list(expected_tokens)))
    _require(count == 3, "generation_count_mismatch", "exactly three fresh runs are required")
    authority = _authenticate_authority(bundle, REPO_ROOT)
    fresh_bundles = [_fresh_bundle(bundle) for _ in range(count)]
    fresh_authorities = []
    for run_index, fresh in enumerate(fresh_bundles):
        _require(fresh is not bundle and fresh.model is not bundle.model,
                 "fresh_bundle_failure", str(run_index + 1))
        fresh_authority = _authenticate_authority(fresh, REPO_ROOT)
        _require(fresh_authority == authority, "fresh_bundle_authority_mismatch",
                 f"fresh bundle {run_index + 1}")
        fresh_authorities.append(fresh_authority)
    authority_state = authority["live_components"]["model_state"]["state_binding"]
    cache = _StaticExportCache(bundle, authority_state)
    contexts = [list(prompt_ids) for _ in range(count)]
    generated_runs: list[list[int]] = [[] for _ in range(count)]
    step_runs: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    canonical_steps: list[dict[str, Any]] = []
    first_mismatch: dict[str, Any] | None = None

    for step_index, expected_token in enumerate(expected_tokens):
        length = len(prompt_ids) + step_index
        cache.export_length(length)
        for run_index, fresh in enumerate(fresh_bundles):
            context = contexts[run_index]
            input_ids = torch.tensor([context], dtype=torch.int64)
            with torch.no_grad():
                eager_outputs = fresh.model._execute(input_ids)
            eager_flat = (
                (eager_outputs[0],) + eager_outputs[1] + eager_outputs[2]
                + eager_outputs[3] + eager_outputs[4]
            )
            exported_flat = cache.replay(input_ids)
            eager = _execution_observations(eager_flat)
            exported = _execution_observations(exported_flat)
            tensor_mismatch = _first_trace_tensor_mismatch(eager_flat, exported_flat)
            mismatch_path = _first_mapping_mismatch(eager, exported)
            eager_token, tie_count = select_greedy_token(eager_outputs[0][0, -1])
            exported_token, exported_tie_count = select_greedy_token(exported_flat[0][0, -1])
            eager_evidence_sha256 = canonical_sha256(eager)
            exported_evidence_sha256 = canonical_sha256(exported)
            context_sha256 = canonical_sha256(context)
            if run_index == 0:
                canonical_step = {
                    "step_index": step_index,
                    "context_length": len(context),
                    "context_sha256": context_sha256,
                    "expected_token": expected_token,
                    "selected_token": eager_token,
                    "tie_count": tie_count,
                    "tie_breaking": "smallest_token_id_among_equal_maxima",
                    "evidence": eager,
                    "evidence_sha256": eager_evidence_sha256,
                    "exported_program_sha256": cache.program_identity(len(context))[
                        "program_sha256"
                    ],
                }
                canonical_step["step_sha256"] = canonical_sha256(canonical_step)
                canonical_steps.append(canonical_step)
            mismatch: dict[str, Any] | None = None
            if tensor_mismatch is not None:
                mismatch = tensor_mismatch
            elif mismatch_path is not None:
                mismatch = {
                    "code": "eager_export_mismatch",
                    "field_path": mismatch_path,
                    "eager": _mapping_path_value(eager, mismatch_path),
                    "exported": _mapping_path_value(exported, mismatch_path),
                }
            elif eager_token != exported_token or tie_count != exported_tie_count:
                mismatch = {
                    "code": "eager_export_token_mismatch",
                    "eager_token": eager_token,
                    "exported_token": exported_token,
                    "eager_tie_count": tie_count,
                    "exported_tie_count": exported_tie_count,
                }
            elif eager_token != expected_token:
                mismatch = {
                    "code": "frozen_token_mismatch",
                    "expected_token": expected_token,
                    "actual_token": eager_token,
                }
            elif run_index > 0:
                canonical = canonical_steps[step_index]
                if (context_sha256 != canonical["context_sha256"]
                        or eager_evidence_sha256 != canonical["evidence_sha256"]
                        or eager_token != canonical["selected_token"]
                        or tie_count != canonical["tie_count"]):
                    mismatch = {
                        "code": "fresh_run_trajectory_mismatch",
                        "canonical_step_ref": step_index,
                        "canonical_evidence_sha256": canonical["evidence_sha256"],
                        "actual_evidence_sha256": eager_evidence_sha256,
                    }
            step_payload: dict[str, Any] = {
                "step_index": step_index,
                "context_length": len(context),
                "canonical_step_ref": step_index,
                "canonical_evidence_ref": f"generation.canonical_steps.{step_index}.evidence",
                "eager_evidence_sha256": eager_evidence_sha256,
                "exported_evidence_sha256": exported_evidence_sha256,
                "tensor_comparison_count": TRACE_TENSORS_PER_STEP,
                "comparison_status": "matched" if mismatch is None else "mismatch",
            }
            if mismatch is not None:
                step_payload["mismatch"] = mismatch
                step_payload["dual_evidence"] = {"eager": eager, "exported": exported}
            step = {**step_payload, "comparison_sha256": canonical_sha256(step_payload)}
            step_runs[run_index].append(step)
            if mismatch is not None:
                first_mismatch = {
                    "run_index": run_index,
                    "step_index": step_index,
                    "context_length": len(context),
                    **mismatch,
                }
                break
            generated_runs[run_index].append(eager_token)
            context.append(eager_token)
        cache.release()
        if first_mismatch is not None:
            break

    runs: list[dict[str, Any]] = []
    for run_index, fresh in enumerate(fresh_bundles):
        steps = step_runs[run_index]
        mode_summaries = {
            mode: _mode_evidence_summary(
                mode,
                [step[f"{mode}_evidence_sha256"] for step in steps],
                TRACE_TENSORS_PER_STEP,
            )
            for mode in ("eager", "exported")
        }
        transcript_payload = {
            "prompt_ids": list(prompt_ids),
            "tokens": generated_runs[run_index],
            "canonical_step_sha256": [
                canonical_steps[index]["step_sha256"] for index in range(len(steps))
            ],
            "step_comparison_sha256": [step["comparison_sha256"] for step in steps],
            "mode_aggregate_sha256": {
                mode: summary["aggregate_sha256"] for mode, summary in mode_summaries.items()
            },
            "model_receipt_sha256": fresh.receipt["receipt_sha256"],
        }
        transcript_sha256 = canonical_sha256(transcript_payload)
        state_binding = fresh_authorities[run_index]["live_components"]["model_state"][
            "state_binding"
        ]
        run = {
            "run_index": run_index,
            "bundle_instance": run_index + 1,
            "bundle_materialization": "fresh_deep_copy_of_authenticated_exact_bundle",
            "model_receipt_sha256": fresh.receipt["receipt_sha256"],
            "model_state_binding": {
                "authority_ref": "identity.live_components.model_state.state_binding",
                "normalized_tensors_sha256": state_binding["normalized_tensors_sha256"],
                "task_2_named_state_sha256": state_binding["task_2_named_state_sha256"],
            },
            "tokens": generated_runs[run_index],
            "steps": steps,
            "modes": mode_summaries,
            "tensor_comparison_count": len(steps) * TRACE_TENSORS_PER_STEP,
            "transcript_sha256": transcript_sha256,
        }
        run["run_sha256"] = canonical_sha256(run)
        runs.append(run)

    transcript_hashes = [run["transcript_sha256"] for run in runs]
    completed_runs = _completed_run_count(runs, expected_tokens, len(expected_tokens))
    matched = first_mismatch is None and completed_runs == count and len(set(transcript_hashes)) == 1
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "matched" if matched else "mismatch",
        "authority": authority,
        "prompt_ids": list(prompt_ids),
        "expected_tokens": list(expected_tokens),
        "context_lengths": list(range(len(prompt_ids), len(prompt_ids) + len(expected_tokens))),
        "execution_modes": ["eager", "torch_export_static_per_length"],
        "canonical_steps": canonical_steps,
        "runs": runs,
        "export_cache": cache.summary(),
        "determinism": {
            "required_runs": count,
            "completed_runs": completed_runs,
            "run_transcript_sha256": transcript_hashes,
            "all_transcripts_identical": (
                len(transcript_hashes) == count and len(set(transcript_hashes)) == 1
            ),
            "three_run_sha256": canonical_sha256(transcript_hashes),
        },
        "first_mismatch": first_mismatch,
        "compiler_model_registration": {
            "performed": False,
            "gate": "open_for_later_lowering" if matched else "closed",
        },
    }
    result["result_sha256"] = canonical_sha256(result)
    return result


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "artifact_sha256"})


def build_artifact(repo_root: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    repo_root = Path(repo_root)
    _require(result.get("status") == "matched" and result.get("first_mismatch") is None,
             "generation_not_matched", "refusing to register mismatched generation evidence")
    identity = copy.deepcopy(result["authority"])
    identity["sources"] = {
        str(relative): _sha256(repo_root / relative)
        for relative in (VERIFIER_RELATIVE, TEST_RELATIVE)
    }
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "exact_generation_matched",
        "identity": identity,
        "generation": copy.deepcopy(dict(result)),
        "claims": {
            "all_sixteen_steps_compared": True,
            "eager_export_observations_bit_exact": True,
            "three_fresh_bundle_runs_deterministic": True,
            "external_oracle_used_as_candidate": False,
            "compiler_model_registered": False,
        },
    }
    artifact["artifact_sha256"] = artifact_sha256(artifact)
    return artifact


def _artifact_require(condition: bool, message: str) -> None:
    _require(condition, "artifact_evidence_mismatch", message)


def _validate_observation(value: Mapping[str, Any], label: str) -> None:
    _artifact_require(len(value.get("checkpoint_sha256", {})) == len(CHECKPOINT_SHAPES), label)
    _artifact_require(len(value.get("qdq_boundary_sha256", {})) == len(QDQ_BOUNDARY_NAMES), label)
    _artifact_require(len(value.get("gemv_accumulator_sha256", {})) == len(GEMV_NAMES), label)
    _artifact_require(
        len(value.get("nonlinear_boundary_sha256", {})) == len(NONLINEAR_BOUNDARY_NAMES), label
    )
    groups = {
        "checkpoint_sha256": value["checkpoint_sha256"],
        "qdq_boundary_sha256": value["qdq_boundary_sha256"],
        "gemv_accumulator_sha256": value["gemv_accumulator_sha256"],
        "nonlinear_boundary_sha256": value["nonlinear_boundary_sha256"],
    }
    _artifact_require(value.get("observation_sha256") == canonical_sha256(groups), label)


def _validate_static_identity(identity: Mapping[str, Any], repo_root: Path) -> None:
    task_2_path = repo_root / TASK_2_ARTIFACT_RELATIVE
    contract_path = repo_root / CONTRACT_RELATIVE
    audit_path = repo_root / AUDIT_RELATIVE
    profile_path = repo_root / PROFILE_RELATIVE
    certificate_path = repo_root / CERTIFICATE_RELATIVE
    oracle_path = repo_root / ORACLE_RELATIVE
    _artifact_require(_sha256(task_2_path) == TASK_2_ARTIFACT_FILE_SHA256, "Task 2 artifact")
    _artifact_require(_sha256(contract_path) == TASK_1_CONTRACT_SHA256, "Task 1 contract")
    _artifact_require(_sha256(audit_path) == TASK_1_AUDIT_FILE_SHA256, "Task 1 audit")
    task_2 = _load_json(task_2_path)
    audit = _load_json(audit_path)
    profile = _load_json(profile_path)
    certificate = _load_json(certificate_path)
    oracle = _load_json(oracle_path)
    expected_task_1 = {
        "commit": TASK_1_COMMIT,
        "contract_file_sha256": TASK_1_CONTRACT_SHA256,
        "audit_file_sha256": TASK_1_AUDIT_FILE_SHA256,
        "audit_payload_sha256": audit["sha256"],
    }
    expected_task_2 = {
        "commit": TASK_2_COMMIT,
        "artifact_file_sha256": TASK_2_ARTIFACT_FILE_SHA256,
        "artifact_sha256": TASK_2_ARTIFACT_SHA256,
        "model_receipt_sha256": TASK_2_MODEL_RECEIPT_SHA256,
        "fixed_logits_oracle_sha256": oracle["oracle_sha256"],
        "fixed_logits_vector_sha256": oracle["logits"]["canonical_sha256"],
        "model_state_sha256": task_2["exported_program"]["state_dict_sha256"],
    }
    _artifact_require(identity.get("task_1") == expected_task_1, "Task 1 identity")
    _artifact_require(identity.get("task_2") == expected_task_2, "Task 2 identity")
    expected_sources = {
        str(relative): _sha256(repo_root / relative)
        for relative in (VERIFIER_RELATIVE, TEST_RELATIVE)
    }
    _artifact_require(identity.get("sources") == expected_sources, "Task 3 sources")

    def component(value: Any, **extra: Any) -> dict[str, Any]:
        return {
            "status": "matched_authenticated_authority",
            "canonical_sha256": canonical_sha256(value),
            **extra,
        }

    oracle_values = oracle["logits"]["values"]
    live = identity.get("live_components", {})
    expected_local = {
        "contract": component(_load_json(contract_path), file_sha256=TASK_1_CONTRACT_SHA256),
        "audit": component(audit, file_sha256=TASK_1_AUDIT_FILE_SHA256,
                           self_sha256=audit["sha256"]),
        "fixed_profile": component(
            profile, file_sha256=_sha256(profile_path), self_sha256=profile["profile_sha256"]
        ),
        "reachable_certificate": component(
            certificate, file_sha256=_sha256(certificate_path),
            self_sha256=certificate["certificate_sha256"],
        ),
        "independent_oracle": component(
            oracle, file_sha256=_sha256(oracle_path), self_sha256=oracle["oracle_sha256"]
        ),
        "oracle_logits": component(
            oracle_values,
            little_endian_int64_sha256=_little_endian_int64_sha256(
                torch.tensor(oracle_values, dtype=torch.int64)
            ),
        ),
        "receipt": {
            "status": "matched_authenticated_authority",
            "canonical_sha256": TASK_2_MODEL_RECEIPT_SHA256,
            "self_sha256": TASK_2_MODEL_RECEIPT_SHA256,
        },
    }
    for name, expected in expected_local.items():
        _artifact_require(live.get(name) == expected, f"live component {name}")
    manifest_identity = task_2["identity"]["package"]["files"]["manifest.json"]
    _artifact_require(live.get("manifest") == {
        "status": "matched_authenticated_authority",
        "canonical_sha256": MANIFEST_CANONICAL_SHA256,
        "file_sha256": manifest_identity["sha256"],
    }, "live component manifest")
    model_component = live.get("model_state", {})
    binding = model_component.get("state_binding", {})
    _artifact_require(binding.get("normalization") == "none", "model state normalization")
    _artifact_require(binding.get("tensor_count") == 257, "model state tensor count")
    _artifact_require(binding.get("normalized_tensors_sha256") == MODEL_STATE_TENSORS_SHA256,
                      "model state tensor manifest")
    _artifact_require(binding.get("task_2_named_state_sha256")
                      == expected_task_2["model_state_sha256"], "model state Task 2 digest")
    _artifact_require(binding.get("canonical_sha256") == canonical_sha256({
        "normalization": binding.get("normalization"), "tensors": binding.get("tensors")
    }), "model state binding hash")
    _artifact_require(binding.get("normalized_tensors_sha256")
                      == canonical_sha256(binding.get("tensors")), "model state tensors hash")
    _artifact_require(model_component == component(binding, state_binding=binding),
                      "live component model state")
    _artifact_require(set(live) == {
        "contract", "audit", "fixed_profile", "reachable_certificate", "manifest",
        "independent_oracle", "oracle_logits", "receipt", "model_state",
    }, "live component closure")


def _validate_generation(value: Mapping[str, Any]) -> None:
    _artifact_require(value.get("schema") == SCHEMA and value.get("status") == "matched",
                      "generation schema/status")
    _artifact_require(value.get("prompt_ids") == PROMPT_IDS, "prompt")
    _artifact_require(value.get("expected_tokens") == FROZEN_TOKENS, "frozen tokens")
    _artifact_require(value.get("context_lengths") == list(range(4, 20)), "contexts")
    _artifact_require(value.get("execution_modes")
                      == ["eager", "torch_export_static_per_length"], "execution modes")
    canonical_steps = value.get("canonical_steps")
    _artifact_require(isinstance(canonical_steps, list) and len(canonical_steps) == 16,
                      "canonical evidence count")
    for index, step in enumerate(canonical_steps):
        evidence = step.get("evidence", {})
        _validate_observation(evidence, f"canonical evidence {index}")
        _artifact_require(step.get("step_index") == index
                          and step.get("context_length") == index + 4
                          and step.get("context_sha256")
                          == canonical_sha256(PROMPT_IDS + FROZEN_TOKENS[:index])
                          and step.get("expected_token") == FROZEN_TOKENS[index]
                          and step.get("selected_token") == FROZEN_TOKENS[index]
                          and step.get("tie_count", 0) >= 1
                          and step.get("tie_breaking")
                          == "smallest_token_id_among_equal_maxima",
                          f"canonical step {index}")
        _artifact_require(step.get("evidence_sha256") == canonical_sha256(evidence),
                          f"canonical evidence hash {index}")
        _artifact_require(step.get("step_sha256") == canonical_sha256({
            key: item for key, item in step.items() if key != "step_sha256"
        }), f"canonical step hash {index}")
        _artifact_require("eager" not in step and "exported" not in step,
                          f"compact canonical step {index}")

    export_cache = value.get("export_cache", {})
    programs = export_cache.get("programs", {})
    _artifact_require(export_cache.get("export_count") == 16
                      and export_cache.get("replay_count") == 48
                      and export_cache.get("strategy")
                      == "one_static_trace_export_per_context_length"
                      and export_cache.get("context_lengths") == list(range(4, 20))
                      and sorted(map(int, programs)) == list(range(4, 20)), "export cache counters")
    authority_binding = value["authority"]["live_components"]["model_state"]["state_binding"]
    for length in range(4, 20):
        program = programs[str(length)]
        normalized = program.get("normalized_state_binding", {})
        _artifact_require(normalized.get("normalization") == "removed_exact_prefix:model."
                          and normalized.get("tensor_count") == authority_binding["tensor_count"]
                          and normalized.get("normalized_tensors_sha256")
                          == authority_binding["normalized_tensors_sha256"]
                          and normalized.get("task_2_named_state_sha256")
                          == authority_binding["task_2_named_state_sha256"]
                          and normalized.get("authority_ref")
                          == "identity.live_components.model_state.state_binding",
                          f"export state binding {length}")
        _artifact_require(normalized.get("canonical_sha256") == canonical_sha256({
            "normalization": "removed_exact_prefix:model.",
            "tensors": authority_binding["tensors"],
        }), f"export normalized state hash {length}")
        program_payload = {
            key: item for key, item in program.items()
            if key not in ("program_sha256", "normalized_state_binding")
        }
        _artifact_require(program.get("program_sha256") == canonical_sha256(program_payload),
                          f"export program {length}")
        _artifact_require(canonical_steps[length - 4].get("exported_program_sha256")
                          == program["program_sha256"], f"step program {length}")
    _artifact_require(export_cache.get("cache_sha256") == canonical_sha256(programs),
                      "export cache hash")

    runs = value.get("runs")
    _artifact_require(isinstance(runs, list) and len(runs) == 3, "run count")
    for run_index, run in enumerate(runs):
        steps = run.get("steps")
        _artifact_require(run.get("run_index") == run_index
                          and run.get("bundle_instance") == run_index + 1
                          and run.get("bundle_materialization")
                          == "fresh_deep_copy_of_authenticated_exact_bundle"
                          and run.get("model_receipt_sha256") == TASK_2_MODEL_RECEIPT_SHA256
                          and run.get("tokens") == FROZEN_TOKENS
                          and isinstance(steps, list) and len(steps) == 16,
                          f"run {run_index}")
        _artifact_require(run.get("model_state_binding") == {
            "authority_ref": "identity.live_components.model_state.state_binding",
            "normalized_tensors_sha256": MODEL_STATE_TENSORS_SHA256,
            "task_2_named_state_sha256": value["authority"]["task_2"][
                "model_state_sha256"
            ],
        }, f"run state binding {run_index}")
        for index, step in enumerate(steps):
            payload = {key: item for key, item in step.items() if key != "comparison_sha256"}
            _artifact_require(step.get("comparison_sha256") == canonical_sha256(payload)
                              and step.get("comparison_status") == "matched"
                              and step.get("canonical_step_ref") == index
                              and step.get("eager_evidence_sha256")
                              == canonical_steps[index]["evidence_sha256"]
                              and step.get("exported_evidence_sha256")
                              == canonical_steps[index]["evidence_sha256"]
                              and step.get("tensor_comparison_count") == TRACE_TENSORS_PER_STEP
                              and "dual_evidence" not in step,
                              f"run {run_index} step {index}")
        modes = run.get("modes", {})
        for mode in ("eager", "exported"):
            expected_mode = _mode_evidence_summary(
                mode, [step[f"{mode}_evidence_sha256"] for step in steps],
                TRACE_TENSORS_PER_STEP,
            )
            _artifact_require(modes.get(mode) == expected_mode, f"run {run_index} {mode}")
        transcript_payload = {
            "prompt_ids": PROMPT_IDS,
            "tokens": FROZEN_TOKENS,
            "canonical_step_sha256": [step["step_sha256"] for step in canonical_steps],
            "step_comparison_sha256": [step["comparison_sha256"] for step in steps],
            "mode_aggregate_sha256": {
                mode: modes[mode]["aggregate_sha256"] for mode in ("eager", "exported")
            },
            "model_receipt_sha256": TASK_2_MODEL_RECEIPT_SHA256,
        }
        _artifact_require(run.get("transcript_sha256") == canonical_sha256(transcript_payload),
                          f"run transcript {run_index}")
        _artifact_require(run.get("tensor_comparison_count") == 16 * TRACE_TENSORS_PER_STEP,
                          f"run tensor comparisons {run_index}")
        _artifact_require(run.get("run_sha256") == canonical_sha256({
            key: item for key, item in run.items() if key != "run_sha256"
        }), f"run hash {run_index}")
    transcripts = [run["transcript_sha256"] for run in runs]
    expected_determinism = {
        "required_runs": 3,
        "completed_runs": 3,
        "run_transcript_sha256": transcripts,
        "all_transcripts_identical": len(set(transcripts)) == 1,
        "three_run_sha256": canonical_sha256(transcripts),
    }
    _artifact_require(value.get("determinism") == expected_determinism, "determinism")
    _artifact_require(value.get("first_mismatch") is None, "unexpected mismatch")
    _artifact_require(value.get("compiler_model_registration") == {
        "performed": False, "gate": "open_for_later_lowering",
    }, "compiler registration")
    _artifact_require(value.get("result_sha256") == canonical_sha256({
        key: item for key, item in value.items() if key != "result_sha256"
    }), "generation result hash")
    _artifact_require(value.get("result_sha256") == AUTHENTICATED_RESULT_SHA256,
                      "authenticated generation result")


def validate_artifact(
    value: Mapping[str, Any], repo_root: Path, expected_result: Mapping[str, Any] | None = None
) -> None:
    repo_root = Path(repo_root)
    if value.get("artifact_sha256") != artifact_sha256(value):
        raise ExactGenerationError("artifact_self_hash_mismatch", "generation artifact")
    _artifact_require(value.get("schema") == SCHEMA
                      and value.get("status") == "exact_generation_matched", "schema/status")
    identity = value.get("identity", {})
    _validate_static_identity(identity, repo_root)
    generation = value.get("generation", {})
    expected_authority = {key: item for key, item in identity.items() if key != "sources"}
    _artifact_require(generation.get("authority") == expected_authority, "authority duplication")
    _validate_generation(generation)
    _artifact_require(value.get("claims") == {
        "all_sixteen_steps_compared": True,
        "eager_export_observations_bit_exact": True,
        "three_fresh_bundle_runs_deterministic": True,
        "external_oracle_used_as_candidate": False,
        "compiler_model_registered": False,
    }, "claims")
    if expected_result is not None:
        _artifact_require(value == build_artifact(repo_root, expected_result),
                          "current identities or execution differ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = load_exact_model(args.contract, args.package, args.model_path)
    result = verify_generation(bundle, PROMPT_IDS, FROZEN_TOKENS, count=3)
    artifact = build_artifact(REPO_ROOT, result)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(artifact["artifact_sha256"])


if __name__ == "__main__":
    main()
