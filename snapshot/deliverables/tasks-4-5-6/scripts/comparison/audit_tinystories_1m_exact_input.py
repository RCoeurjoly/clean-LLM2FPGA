#!/usr/bin/env python3
from __future__ import annotations

"""Audit whether the kev-gpt TinyStories package defines an exact compiler input."""

import argparse
import hashlib
import json
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
QDQ_RECEIPT = ROOT / "artifacts/reference/tinystories-1m-qdq-semantics.json"
IMPLEMENTATION_PROFILE = ROOT / "artifacts/reference/tinystories-1m-implementation-profile.json"
FIXED_PROFILE = ROOT / "artifacts/reference/tinystories-1m-fixed-hardware-qdq-profile.json"
REFERENCE_SOURCES = (
    "flake.nix",
    "tinystories/__init__.py",
    "tinystories/quantize.py",
    "tinystories/build_package.py",
    "tinystories/gptneo_schema.py",
    "tinystories/import_gptneo.py",
    "tinystories/int_reference.py",
    "tinystories/package_io.py",
    "tinystories/rtl_memories.py",
    "tinystories/hardware_reference.py",
    "tinystories/write_rtl_fixture.py",
    "fpga/rtl/async_fifo.sv",
    "fpga/rtl/bscan_packet_endpoint.sv",
    "fpga/rtl/gptneo_attention.sv",
    "fpga/rtl/gptneo_gelu.sv",
    "fpga/rtl/gptneo_resident_gemv.sv",
    "fpga/rtl/gptneo_iterative_divider.sv",
    "fpga/rtl/gptneo_layernorm.sv",
    "fpga/rtl/gptneo_sequencer.sv",
    "fpga/rtl/tinystories_interactive_top.sv",
    "fpga/rtl/tinystories_packet_controller.sv",
    "host/kevin_jtag_cli.py",
)
FROZEN_PROMPT_IDS = [7454, 2402, 257, 640]
FROZEN_EXPECTED_TOKENS = [
    11, 612, 373, 257, 1310, 2576, 3706, 20037,
    13, 1375, 6151, 284, 711, 2354, 287, 262,
]


class IdentityFrontierError(ValueError):
    """A named reason why the audit cannot authenticate the exact input."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(kev_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(kev_root), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _git_bytes(kev_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(kev_root), *args], check=True, capture_output=True
    ).stdout


def _canonical_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_finite_adapter_input(value: Any) -> None:
    """Reject non-finite values before they can enter the integer datapath."""

    if isinstance(value, bool):
        raise ValueError("adapter input must be integer token IDs, not booleans")
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("adapter input contains a non-finite value")
        raise ValueError("adapter input must be integer token IDs")
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_finite_adapter_input(item)
        return
    raise ValueError("adapter input must be integer token IDs")


def _validate_finite_package_values(package_path: Path, manifest: dict[str, Any]) -> None:
    """The fixed profile has no NaN/Inf representation after materialization."""

    for file_name in ("scales.bin",):
        payload = (package_path / file_name).read_bytes()
        _require(len(payload) % 4 == 0, f"{file_name} is not float32-aligned")
        for (value,) in struct.iter_unpack("<f", payload):
            _require(math.isfinite(value), f"package {file_name} contains a non-finite value")
    image = (package_path / "weights.bin").read_bytes()
    tensors = manifest.get("tensors")
    _require(isinstance(tensors, dict), "manifest tensor table is missing")
    for name, descriptor in tensors.items():
        if not isinstance(descriptor, dict) or descriptor.get("format") != "float32":
            continue
        offset = descriptor.get("offset")
        nbytes = descriptor.get("nbytes")
        _require(isinstance(offset, int) and isinstance(nbytes, int) and nbytes % 4 == 0,
                 f"manifest float32 tensor is malformed: {name}")
        _require(0 <= offset <= len(image) and offset + nbytes <= len(image),
                 f"manifest float32 tensor is out of range: {name}")
        for (value,) in struct.iter_unpack("<f", image[offset:offset + nbytes]):
            _require(math.isfinite(value), f"package tensor contains a non-finite value: {name}")


def _pinned_source_closure(kev_root: Path, deployed: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Authenticate every semantic source as a blob in the selected commit."""

    revision = deployed["revision"]
    try:
        _git(kev_root, "cat-file", "-e", f"{revision}^{{commit}}")
    except (OSError, subprocess.SubprocessError) as error:
        raise IdentityFrontierError(
            "pinned_commit_unavailable",
            f"pinned deployed commit is unavailable: {revision}",
        ) from error
    expected = deployed["sources"]
    closure: dict[str, dict[str, str]] = {}
    for name in REFERENCE_SOURCES:
        try:
            blob = _git(kev_root, "rev-parse", f"{revision}:{name}")
            payload = _git_bytes(kev_root, "cat-file", "blob", blob)
        except (OSError, subprocess.SubprocessError) as error:
            raise IdentityFrontierError(
                "pinned_source_identity_mismatch",
                f"pinned semantic source is unavailable: {name}",
            ) from error
        identity = {"git_blob_sha1": blob, "sha256": hashlib.sha256(payload).hexdigest()}
        if expected.get(name) != identity:
            raise IdentityFrontierError(
                "pinned_source_identity_mismatch",
                f"pinned semantic source identity mismatch: {name}",
            )
        closure[name] = identity
    return closure


def _worktree_observability(kev_root: Path) -> dict[str, Any]:
    all_status = _git(kev_root, "status", "--short", "--untracked-files=all")
    relevant_status = _git(kev_root, "status", "--short", "--", *REFERENCE_SOURCES)
    relevant_diff = _git_bytes(
        kev_root, "diff", "--binary", "HEAD", "--", *REFERENCE_SOURCES
    )
    return {
        "reference_source_worktree_clean": not bool(all_status),
        "worktree_status": all_status.splitlines(),
        "relevant_dirty_paths": [line.split(maxsplit=1)[1] for line in relevant_status.splitlines()],
        "relevant_worktree_diff_sha256": (
            hashlib.sha256(relevant_diff).hexdigest() if relevant_diff else None
        ),
    }


def _run_fixed_reference(
    kev_root: Path,
    revision: str,
    package_path: Path,
    prompt_ids: list[int],
    manifest: dict[str, Any] | None = None,
) -> list[int]:
    """Run the already-authenticated revision after call-boundary finite checks."""

    if manifest is None:
        manifest = _load_json(package_path / "manifest.json")
    program = """\
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tinystories.hardware_reference import FixedGPTNeo
print(json.dumps(FixedGPTNeo(Path(sys.argv[2])).generate(json.loads(sys.argv[3]), 16)))
"""
    with tempfile.TemporaryDirectory() as temporary:
        source_root = Path(temporary)
        package_root = source_root / "tinystories"
        package_root.mkdir()
        for name in (
            "tinystories/__init__.py",
            "tinystories/int_reference.py",
            "tinystories/hardware_reference.py",
        ):
            (source_root / name).write_bytes(_git_bytes(kev_root, "show", f"{revision}:{name}"))
        # These checks are deliberately adjacent to the only FixedGPTNeo call.
        try:
            _validate_finite_package_values(package_path, manifest)
        except ValueError as error:
            raise IdentityFrontierError("nonfinite_package_value", str(error)) from error
        try:
            validate_finite_adapter_input(prompt_ids)
        except ValueError as error:
            raise IdentityFrontierError("nonfinite_adapter_input", str(error)) from error
        try:
            result = subprocess.run(
                [sys.executable, "-c", program, str(source_root), str(package_path), json.dumps(prompt_ids)],
                check=True,
                text=True,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IdentityFrontierError(
                "reference_execution_failure",
                f"fixed reference execution failed: {type(error).__name__}",
            ) from error
    try:
        tokens = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise IdentityFrontierError(
            "reference_execution_failure",
            "fixed reference returned invalid JSON",
        ) from error
    if not (
        isinstance(tokens, list)
        and all(isinstance(token, int) and not isinstance(token, bool) for token in tokens)
    ):
        raise IdentityFrontierError(
            "reference_execution_failure",
            "fixed reference returned malformed tokens",
        )
    return tokens


def _validate_deployed_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the accepted-spec selection without reinterpreting old receipts."""

    model = contract.get("model")
    package = contract.get("package")
    tokenizer = contract.get("tokenizer")
    quantization = contract.get("quantization")
    fixed_point = contract.get("fixed_point")
    reference = contract.get("reference")
    deployed = contract.get("deployed_profile")
    selection_authority = contract.get("selection_authority")
    _require(isinstance(model, dict), "contract model identity is missing")
    _require(isinstance(package, dict) and isinstance(package.get("files"), dict),
             "contract complete package identity is missing")
    _require(isinstance(tokenizer, dict), "contract tokenizer identity is missing")
    _require(isinstance(quantization, dict), "contract quantization profile is missing")
    _require(isinstance(fixed_point, dict), "contract fixed-point profile is missing")
    _require(isinstance(reference, dict), "contract reference fixture is missing")
    _require(isinstance(deployed, dict), "contract deployed profile is missing")
    _require(isinstance(selection_authority, dict), "contract selection authority is missing")
    selection_path = selection_authority.get("path")
    _require(isinstance(selection_path, str)
             and selection_authority.get("sha256") == _sha256(ROOT / selection_path),
             "accepted-spec selection authority mismatch")
    _require(model.get("source_revision") == "ac533fb8b4f69c71894bf96badfe11e6294d9fcf",
             "contract model revision differs from the accepted spec")
    _require(tokenizer.get("sha256") == package["files"].get("tokenizer.json", {}).get("sha256"),
             "contract tokenizer hash is not bound to the complete package")
    _require(quantization == {
        "weights": "symmetric per-output INT8",
        "activations": "symmetric per-channel INT8",
        "activation_boundary_count": 97,
        "activation_vector_widths": [64, 256],
        "scale_format": "little-endian float32",
    }, "contract quantization profile differs from the deployed package")
    _require(fixed_point == {
        "value_format": "signed Q16.16",
        "scale_format": "unsigned Q8.24",
        "gemv_accumulator": "signed 64-bit serial accumulator",
    }, "contract fixed-point profile differs from the accepted spec")
    _require(reference.get("prompt_tokens") == FROZEN_PROMPT_IDS
             and reference.get("tokens") == FROZEN_EXPECTED_TOKENS
             and reference.get("generation") == "greedy top-1",
             "contract frozen generation fixture differs from the accepted spec")
    _require(deployed.get("name") == "fixed_hardware_reference"
             and deployed.get("input_domain") == "finite_integer_or_fixed_point_only"
             and deployed.get("nonfinite_policy") == "reject_nonfinite_adapter_input",
             "contract deployed profile or non-finite policy is not fail closed")
    sources = deployed.get("sources")
    _require(isinstance(sources, dict) and set(sources) == set(REFERENCE_SOURCES),
             "contract deployed source set is incomplete")
    return deployed


def audit_exact_input(
    contract_path: Path, package_path: Path, kev_root: Path
) -> dict[str, Any]:
    contract = _load_json(contract_path)
    manifest = _load_json(package_path / "manifest.json")
    receipt = _load_json(package_path / "receipt.json")
    qdq = _load_json(QDQ_RECEIPT)
    implementation = _load_json(IMPLEMENTATION_PROFILE)
    fixed_profile = _load_json(FIXED_PROFILE)
    deployed = _validate_deployed_contract(contract)
    semantic_authorities = contract.get("semantic_authorities")
    _require(isinstance(semantic_authorities, dict), "contract semantic authorities are missing")
    expected_receipts = {
        "qdq": QDQ_RECEIPT,
        "implementation_profile": IMPLEMENTATION_PROFILE,
        "fixed_profile": FIXED_PROFILE,
    }
    for name, path in expected_receipts.items():
        authority = semantic_authorities.get(name)
        _require(isinstance(authority, dict)
                 and authority.get("path") == str(path.relative_to(ROOT))
                 and authority.get("sha256") == _sha256(path),
                 f"semantic receipt identity mismatch: {name}")

    receipt_files = receipt.get("files")
    if not isinstance(receipt_files, dict):
        raise ValueError("package receipt lacks files")
    package_files: dict[str, dict[str, Any]] = {}
    for name, expected in sorted(receipt_files.items()):
        if not isinstance(expected, dict):
            raise ValueError(f"package receipt entry {name} is malformed")
        path = package_path / name
        actual = {"sha256": _sha256(path), "size": path.stat().st_size}
        if actual != expected:
            raise ValueError(f"package file identity mismatch: {name}")
        package_files[name] = actual

    manifest_hash = _sha256(package_path / "manifest.json")
    if receipt.get("manifest_sha256") != manifest_hash:
        raise ValueError("package manifest receipt mismatch")
    package_contract = contract.get("package")
    if not isinstance(package_contract, dict):
        raise ValueError("contract package identity is missing")
    if package_contract.get("manifest_sha256") != manifest_hash:
        raise ValueError("contract manifest identity mismatch")
    if package_contract.get("sha256") != _sha256(package_path / "weights.bin"):
        raise ValueError("contract weight identity mismatch")
    if package_contract.get("files") != package_files:
        raise ValueError("contract complete package identity mismatch")

    activation_scales = manifest.get("activation_scales")
    if not isinstance(activation_scales, dict):
        raise ValueError("manifest activation_scales is missing")
    widths: set[int] = set()
    for name, values in sorted(activation_scales.items()):
        if not isinstance(name, str) or not isinstance(values, list) or not values:
            raise ValueError(f"activation scale vector is malformed: {name!r}")
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
            for value in values
        ):
            raise ValueError(f"activation scale vector is invalid: {name}")
        widths.add(len(values))
    quantization = contract.get("quantization")
    if not isinstance(quantization, dict):
        raise ValueError("contract quantization is missing")
    activation_label = quantization.get("activations")
    activation_granularity = (
        "per_channel" if activation_label == "symmetric per-channel INT8" else "conflicting"
    )

    conflicts: list[dict[str, str]] = []
    if activation_granularity != "per_channel":
        conflicts.append(
            {
                "code": "activation_scale_granularity_conflict",
                "reason": "contract activation label disagrees with 97 package scale vectors",
            }
        )
    worktree_observability = _worktree_observability(kev_root)
    pinned_closure: dict[str, dict[str, str]] = {}
    generated_tokens: list[int] | None = None
    try:
        pinned_closure = _pinned_source_closure(kev_root, deployed)
        if fixed_profile.get("profile") != deployed["name"]:
            raise IdentityFrontierError(
                "fixed_profile_mismatch", "fixed hardware profile selection mismatch"
            )
        if (
            fixed_profile.get("semantics", {}).get("execution_domain")
            != "integers_only_after_materialization"
        ):
            raise IdentityFrontierError(
                "fixed_profile_mismatch",
                "fixed hardware profile is not an integer/fixed-point domain",
            )
        if (
            fixed_profile.get("semantics", {})
            .get("accumulation", {})
            .get("synthesizable_rtl", {})
            .get("logical_width_bits")
            != 64
        ):
            raise IdentityFrontierError(
                "fixed_profile_mismatch",
                "fixed hardware profile does not use signed 64-bit GEMV accumulation",
            )
        generated_tokens = _run_fixed_reference(
            kev_root, deployed["revision"], package_path, FROZEN_PROMPT_IDS, manifest
        )
        if generated_tokens != FROZEN_EXPECTED_TOKENS:
            raise IdentityFrontierError(
                "reference_token_mismatch", "deployed fixed reference token fixture mismatch"
            )
    except IdentityFrontierError as error:
        conflicts.append({
            "code": error.code,
            "reason": error.reason,
        })
    except (OSError, subprocess.SubprocessError) as error:
        conflicts.append({
            "code": "reference_execution_failure",
            "reason": f"fixed reference execution failed: {type(error).__name__}",
        })
    source = {
        "head_revision": _git(kev_root, "rev-parse", "HEAD"),
        "package_revision": _git(
            kev_root, "log", "-1", "--format=%H", "--", "model_packages/tinystories-1m"
        ),
        "pinned_semantic_source_closure": pinned_closure,
        **worktree_observability,
    }

    result: dict[str, Any] = {
        "schema": "tinystories-1m-exact-input-audit-v1",
        "status": "authenticated" if not conflicts else "identity_frontier",
        "contract": {
            "path": str(contract_path),
            "sha256": _sha256(contract_path),
            "activation_granularity": activation_granularity,
        },
        "model": contract["model"],
        "package": {
            "path": str(package_path),
            "manifest_sha256": manifest_hash,
            "files": package_files,
        },
        "activation_quantization": {
            "format": manifest.get("activation_format"),
            "granularity": "per_channel",
            "boundary_count": len(activation_scales),
            "vector_widths": sorted(widths),
        },
        "tokenizer": contract["tokenizer"],
        "quantization": contract["quantization"],
        "fixed_point": contract["fixed_point"],
        "source": source,
        "semantic_receipts": {
            "qdq": {"path": str(QDQ_RECEIPT.relative_to(ROOT)), "status": qdq.get("status"), "sha256": _sha256(QDQ_RECEIPT), "contract_sha256": semantic_authorities["qdq"]["sha256"]},
            "implementation_profile": {"path": str(IMPLEMENTATION_PROFILE.relative_to(ROOT)), "status": implementation.get("status"), "sha256": _sha256(IMPLEMENTATION_PROFILE), "contract_sha256": semantic_authorities["implementation_profile"]["sha256"]},
            "fixed_profile": {"path": str(FIXED_PROFILE.relative_to(ROOT)), "status": fixed_profile.get("status"), "sha256": _sha256(FIXED_PROFILE), "contract_sha256": semantic_authorities["fixed_profile"]["sha256"]},
        },
        "implementation_profile": {
            "name": deployed["name"],
            "selection": "accepted_spec_deployed_reference_and_synthesizable_rtl",
            "semantics_source": str(FIXED_PROFILE.relative_to(ROOT)),
            "semantics_sha256": _sha256(FIXED_PROFILE),
            "historical_selector_status": implementation.get("status"),
        },
        "nonfinite_policy": deployed["nonfinite_policy"],
        "reference_fixture": {
            "prompt_ids": FROZEN_PROMPT_IDS,
            "expected_tokens": FROZEN_EXPECTED_TOKENS,
            "fixed_reference_tokens": generated_tokens,
        },
        "observability_limitations": [{
            "code": "board_checkpoint_trace_unavailable",
            "classification": "observability_limitation_not_identity_conflict",
            "reason": "No internal board checkpoint trace is claimed; package/source identity and the frozen deployed token fixture are authenticated independently.",
        }],
        "conflicts": conflicts,
        "next_gate": (
            "exact_quantized_pytorch_model"
            if not conflicts
            else "resolve_all_identity_frontier_conflicts"
        ),
    }
    result["sha256"] = _canonical_sha256(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--kev-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit_exact_input(args.contract, args.package, args.kev_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(result["status"])


if __name__ == "__main__":
    main()
