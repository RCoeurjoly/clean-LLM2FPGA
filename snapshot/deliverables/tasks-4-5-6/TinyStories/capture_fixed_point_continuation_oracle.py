#!/usr/bin/env python3
"""Sixteen-step successor fixture using the unchanged exact-model observer.

Step anchors were frozen from a separate authenticated capture on 2026-09-09,
raw receipt 9b69b4a867873dae9cac977f1aca6545b0a2b1e4987c40c7bd4142f53f695417.
They are validation data, never inputs to model computation. The old two-step
fixture and capture source remain immutable authorities for the shared prefix.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from TinyStories import capture_fixed_point_model_token_step_oracle as capture

EXPECTED_STEP_HASHES = (
    "d65e41b23585938ce5118128adeaf05612bcdf9772fcf9170f16d659c2d7beae",
    "ad18da108b924945d908b12f58494904080af984224e536a6b17d6f8d3dbd8c4",
    "71e9b7e301b04a1c5cddd4c03bef0c9879aee6e1ce473f15fd4d61d5c15d8cb1",
    "188cb28b9c7720f9c5e6f330c303613ffc16c22f5e42b1af89f78bc8c910e0b8",
    "14f439017497485820c54276cc98a52c6c2fa68f6996d8e59a465e7b0c69dbd1",
    "b4da3d2d07aa89509ebf24317e66d3bdaf7b52d3cb7bf11b33a71b61dcb31c69",
    "6b8166b02d409cf0414a8d4772dd0cd9a5642d89d1c0c4e8f75822d32ce2a9e1",
    "a30dde1651316a43dee0b690e5ba3796a1fcda676fa83fbc451acd0888772bbc",
    "93b0775e41cce8e75e08bed7aad46dfdbca026f29fc78c1fd287dd9141dc0126",
    "95a1872e8c340cb62b21311d0f69d588435968141b2dcd539c5f9d87924db95c",
    "9ffd42d2ba4021877ab13b3eb40c7a00a4cfbf3a22726c52e5edc0feb355143e",
    "c110dc1029c2ba91aff0beba4c0fc01ef71f05306e33915772f78bc73a4230a5",
    "e226e3d7ca2dccfe7cfee59f450af966dce51309ba816a6350bd6107999b353e",
    "600802bb18e4fac6c334cb170376f8aa52340c1e8181d12fa44a5586fe73e0d5",
    "31050946b44e6bcc7c626019dcbadd170863a8a5b11abd06438740fd2c9615cd",
    "049426bf68af5dfbf3fc988373b7d3cb10f68342917eb7539de9d35fe9f2fedb",
)
DEFAULT_OUTPUT = ROOT / "artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json"


def _template():
    legacy = capture._load_json(ROOT / "artifacts/reference/tinystories-1m-fixed-point-model-token-step-oracle.json")
    capture.verify_oracle_fixture(legacy)
    value = copy.deepcopy(legacy)
    value.pop("artifact_sha256")
    value.pop("steps")
    value["schema"] = "tinystories-1m-fixed-point-continuation-oracle-v1"
    value["status"] = "authenticated_exact_sixteen_token_model_oracle"
    value["scope"] = "exact fixed-point sixteen-token semantic oracle; no RTL or board claim"
    value["identity"]["legacy_artifact_sha256"] = legacy["artifact_sha256"]
    value["identity"]["continuation_capture_sha256"] = capture._sha256(Path(__file__))
    value["token_contract"]["step_count"] = 16
    value["token_contract"]["forbidden_host_inputs"] = ["generated_tokens", "intermediate_states"]
    value["claims"] = {
        "authenticated_package_used": True, "datapath_semantics_modified": False,
        "sixteen_tokens_selected_greedily": True, "feedback_is_computed_greedy_token": True,
        "host_intermediate_inputs_accepted": False, "rtl_generated": False, "board_executed": False}
    return value, legacy


def verify_continuation_oracle(value):
    expected, legacy = _template()
    envelope = {k: v for k, v in value.items() if k not in ("steps", "artifact_sha256")}
    if envelope != expected:
        raise ValueError("continuation identity or contract mismatch")
    if value.get("artifact_sha256") != capture.canonical_sha256(
            {k: v for k, v in value.items() if k != "artifact_sha256"}):
        raise ValueError("continuation self hash mismatch")
    steps = value.get("steps")
    if not isinstance(steps, list) or len(steps) != 16:
        raise ValueError("exactly sixteen steps required")
    generation = capture._load_generation_verifier(capture.DEFAULT_GENERATION)
    context = list(capture.PROMPT_TOKENS)
    for index, step in enumerate(steps):
        digest = capture.canonical_sha256({k: v for k, v in step.items() if k != "step_sha256"})
        if digest != EXPECTED_STEP_HASHES[index] or step.get("step_sha256") != digest:
            raise ValueError(f"authenticated semantic checkpoint mismatch at step {index}")
        canonical = generation["generation"]["canonical_steps"][index]
        if (step["step_index"] != index or step["context_tokens"] != context
                or step["context_length"] != len(context)
                or step["selected_token"]["value"] != canonical["selected_token"]
                or step["predecessor"]["canonical_step_sha256"] != canonical["step_sha256"]):
            raise ValueError(f"generation predecessor or feedback mismatch at step {index}")
        context.append(step["selected_token"]["value"])
    if steps[:2] != legacy["steps"]:
        raise ValueError("legacy two-step prefix mismatch")


def build_continuation_oracle(*, host_generated_tokens=None, host_intermediate_states=None):
    if host_generated_tokens is not None or host_intermediate_states is not None:
        raise ValueError("host generated tokens and intermediate states are forbidden")
    generation = capture._load_generation_verifier(capture.DEFAULT_GENERATION)
    bundle = capture.exact_adapter.load_successor_exact_model(
        capture.DEFAULT_CONTRACT, capture.DEFAULT_PACKAGE,
        capture.DEFAULT_MODEL, capture.DEFAULT_GENERATION)
    value, _ = _template()
    context = list(capture.PROMPT_TOKENS)
    steps = []
    for index in range(16):
        step = capture._capture_step(bundle.model, context, index,
                                    generation["generation"]["canonical_steps"][index])
        steps.append(step)
        context.append(step["selected_token"]["value"])
    value["steps"] = steps
    value["artifact_sha256"] = capture.canonical_sha256(value)
    verify_continuation_oracle(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    value = build_continuation_oracle()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"status": value["status"], "artifact_sha256": value["artifact_sha256"]}))


if __name__ == "__main__":
    main()

