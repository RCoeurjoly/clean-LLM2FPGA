#!/usr/bin/env python3
"""Generate the audit-free, streamed-argmax TinyStories production kernel."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE_PATH = ROOT / "scripts/pipeline/lower_fixed_point_model_to_calyx.py"
spec = importlib.util.spec_from_file_location("fixed_point_model_base", BASE_PATH)
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)

CalyxArtifact = base.CalyxArtifact
ORACLE = base.ORACLE


def generate_production_kernel(oracle_path: Path = ORACLE, *, host_second_token=None, host_intermediate_states=None, generation_steps: int = 2, audit_checkpoints: bool = False):
    return base.generate_model_kernel(
        oracle_path,
        host_second_token=host_second_token,
        host_intermediate_states=host_intermediate_states,
        production=True,
        generation_steps=generation_steps,
        audit_checkpoints=audit_checkpoints,
    )


if __name__ == "__main__":
    artifact = generate_production_kernel()
    print(artifact.provenance["futil_sha256"])
