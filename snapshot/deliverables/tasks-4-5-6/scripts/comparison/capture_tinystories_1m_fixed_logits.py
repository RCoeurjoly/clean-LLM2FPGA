#!/usr/bin/env python3
"""Capture frozen-prompt logits from authenticated pinned kev-gpt source blobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


CONTRACT_SHA256 = "859fe3095a4842e413ee99466f5dc63d5420d0e890a3dce0cf7a52e3bd2d1d3c"
AUDIT_FILE_SHA256 = "3cf8a5b9db8acf0ca04e92277c0f9f07c81900a4c754626183bd1d22063616bd"
REVISION = "df1fc45b2ffcb26fddc19cfd57621e7eedf6153f"
PROMPT = [7454, 2402, 257, 640]
SOURCES = (
    "tinystories/__init__.py",
    "tinystories/int_reference.py",
    "tinystories/hardware_reference.py",
)


class FixedLogitsCaptureError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise FixedLogitsCaptureError(code, message)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def oracle_sha256(value: Mapping[str, Any]) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != "oracle_sha256"})


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise FixedLogitsCaptureError("pinned_source_unavailable", " ".join(args)) from error


def _git_text(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode().strip()


def capture_fixed_logits(contract_path: Path, audit_path: Path, package: Path,
                         reference_root: Path) -> dict[str, Any]:
    contract_path, audit_path, package, reference_root = map(
        Path, (contract_path, audit_path, package, reference_root)
    )
    _require(_sha256(contract_path) == CONTRACT_SHA256,
             "contract_identity_mismatch", str(contract_path))
    _require(_sha256(audit_path) == AUDIT_FILE_SHA256,
             "audit_identity_mismatch", str(audit_path))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    _require(audit.get("status") == "authenticated", "identity_frontier", "audit")
    _require(contract["deployed_profile"]["revision"] == REVISION,
             "source_revision_mismatch", REVISION)
    _require(Path(contract["package"]["origin"]).resolve() == package.resolve(),
             "package_identity_mismatch", str(package))
    for name, identity in contract["package"]["files"].items():
        path = package / name
        _require(path.is_file() and path.stat().st_size == identity["size"]
                 and _sha256(path) == identity["sha256"],
                 "package_identity_mismatch", name)

    closure = contract["deployed_profile"]["sources"]
    audit_closure = audit["source"]["pinned_semantic_source_closure"]
    source_identity: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "tinystories").mkdir()
        for name in SOURCES:
            expected = closure[name]
            _require(expected == audit_closure[name], "source_identity_mismatch", name)
            blob = _git_text(reference_root, "rev-parse", f"{REVISION}:{name}")
            payload = _git_bytes(reference_root, "cat-file", "blob", blob)
            actual = {"git_blob_sha1": blob, "sha256": hashlib.sha256(payload).hexdigest()}
            _require(actual == expected, "source_identity_mismatch", name)
            (root / name).write_bytes(payload)
            source_identity[name] = actual
        program = """\
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tinystories.hardware_reference import FixedGPTNeo
values = FixedGPTNeo(Path(sys.argv[2])).forward(json.loads(sys.argv[3]))[-1]
print(json.dumps([int(value) for value in values.tolist()], separators=(\",\", \":\")))
"""
        try:
            completed = subprocess.run(
                [sys.executable, "-c", program, str(root), str(package), json.dumps(PROMPT)],
                check=True, text=True, capture_output=True,
            )
            values = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise FixedLogitsCaptureError("reference_execution_failure", type(error).__name__) from error
    _require(isinstance(values, list) and len(values) == 50257
             and all(isinstance(value, int) and not isinstance(value, bool) for value in values),
             "reference_logits_malformed", "expected 50,257 signed integers")
    packed = b"".join(struct.pack("<q", value) for value in values)
    result: dict[str, Any] = {
        "schema": "tinystories-1m-fixed-logits-oracle-v1",
        "status": "independent_pinned_fixed_reference",
        "identity": {
            "contract_sha256": _sha256(contract_path),
            "audit_file_sha256": _sha256(audit_path),
            "audit_payload_sha256": audit["sha256"],
            "package_manifest_sha256": _sha256(package / "manifest.json"),
            "package_weights_sha256": _sha256(package / "weights.bin"),
            "package_scales_sha256": _sha256(package / "scales.bin"),
            "reference_revision": REVISION,
            "pinned_sources": source_identity,
            "capture_script_sha256": _sha256(Path(__file__)),
        },
        "prompt_tokens": PROMPT,
        "logits": {
            "shape": [50257],
            "dtype": "signed_q16.16_int64",
            "values": values,
            "canonical_sha256": canonical_sha256(values),
            "little_endian_int64_sha256": hashlib.sha256(packed).hexdigest(),
        },
        "next_token": max(range(len(values)), key=values.__getitem__),
    }
    result["oracle_sha256"] = oracle_sha256(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = capture_fixed_logits(
        args.contract, args.audit, args.package, args.reference_root
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(value["logits"]["canonical_sha256"])


if __name__ == "__main__":
    main()
