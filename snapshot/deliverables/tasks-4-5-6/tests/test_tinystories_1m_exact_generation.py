"""Tests for the authenticated exact TinyStories-1M generation gate."""

from __future__ import annotations

import importlib.util
import copy
import hashlib
import json
import unittest
from pathlib import Path

import torch

from TinyStories.model_adapter_exact_package import load_exact_model


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "artifacts/reference/tinystories-1m-exact-input-contract.json"
PACKAGE = Path("/home/roland/kev-gpt/.worktrees/kintex-selftest/model_packages/tinystories-1m")
MODEL = Path(
    "/home/roland/.cache/huggingface/hub/"
    "models--roneneldan--TinyStories-1M/snapshots/"
    "77f1b168e219585646439073245fe87e56b3023e"
)
VERIFIER = ROOT / "scripts/comparison/verify_tinystories_1m_exact_generation.py"
ARTIFACT = ROOT / "artifacts/reference/tinystories-1m-exact-generation.json"
TASK_1_COMMIT = "c8eb2011ee09c368accdf8efd67c50dfc5564679"
TASK_2_COMMIT = "a2eda783819bbfb35a833d1a45d3dd125a176973"
EXPECTED = [11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375, 6151, 284, 711, 2354, 287, 262]


def canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def independent_named_state_sha256(values: dict[str, torch.Tensor]) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(values.items()):
        value = tensor.detach().cpu().contiguous()
        digest.update(name.encode())
        digest.update(str(value.dtype).encode())
        digest.update(json.dumps(list(value.shape), separators=(",", ":")).encode())
        digest.update(value.numpy().tobytes())
    return digest.hexdigest()


def load_script(path: Path):
    spec = importlib.util.spec_from_file_location("exact_generation_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TinyStories1MExactGenerationUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = load_script(VERIFIER)

    def test_normalized_export_state_requires_exact_authenticated_tensor_manifest(self) -> None:
        model = {
            "weight": torch.tensor([[1, 2], [3, 4]], dtype=torch.int64),
            "bias": torch.tensor([5], dtype=torch.int64),
        }
        exported = {f"model.{name}": tensor.clone() for name, tensor in model.items()}
        authority = self.verifier._canonical_state_binding(model)
        actual = self.verifier._canonical_state_binding(exported, wrapper_prefix="model.")

        self.verifier._require_normalized_state_match(actual, authority, "synthetic")
        changed = copy.deepcopy(actual)
        changed["tensors"][1]["bytes_sha256"] = "0" * 64
        changed["normalized_tensors_sha256"] = canonical_sha256(changed["tensors"])
        with self.assertRaisesRegex(ValueError, "export_state_binding_mismatch"):
            self.verifier._require_normalized_state_match(changed, authority, "synthetic")

    def test_mode_aggregate_binds_a_changed_later_step_observation(self) -> None:
        evidence = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(16)]
        first = self.verifier._mode_evidence_summary("eager", evidence, 386)
        changed = list(evidence)
        changed[14] = "0" * 64
        second = self.verifier._mode_evidence_summary("eager", changed, 386)

        self.assertEqual(first["step_count"], 16)
        self.assertEqual(first["tensor_observation_count"], 16 * 386)
        self.assertNotEqual(first["aggregate_sha256"], second["aggregate_sha256"])


@unittest.skipUnless(PACKAGE.is_dir() and MODEL.is_dir(), "frozen package/model inputs unavailable")
class TinyStories1MExactGenerationAuthorityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = load_script(VERIFIER)
        cls.bundle = load_exact_model(CONTRACT, PACKAGE, MODEL)

    def test_mutated_in_memory_model_state_fails_before_generation(self) -> None:
        changed = self.verifier._fresh_bundle(self.bundle)
        first_buffer = next(changed.model.buffers())
        first_buffer.view(-1)[0] += 1

        with self.assertRaisesRegex(ValueError, "task_2_model_state_mismatch"):
            self.verifier.verify_generation(
                changed, [7454, 2402, 257, 640], EXPECTED, count=3
            )

    def test_first_checkpoint_mismatch_names_the_checkpoint(self) -> None:
        eager = {"checkpoint_sha256": {"block.input": "a", "block.output": "b"}}
        exported = {"checkpoint_sha256": {"block.input": "a", "block.output": "changed"}}

        self.assertEqual(
            self.verifier._first_mapping_mismatch(eager, exported),
            ["checkpoint_sha256", "block.output"],
        )

    def test_completed_runs_excludes_every_partial_or_mismatched_run(self) -> None:
        matched_step = {"comparison_status": "matched"}
        runs = [
            {"tokens": EXPECTED, "steps": [matched_step] * 16},
            {"tokens": EXPECTED, "steps": [matched_step] * 15},
            {"tokens": EXPECTED[:-1], "steps": [matched_step] * 16},
            {"tokens": EXPECTED, "steps": [matched_step] * 15 + [{"comparison_status": "mismatch"}]},
        ]

        self.assertEqual(self.verifier._completed_run_count(runs, EXPECTED, 16), 1)

    def test_all_live_bundle_components_require_complete_disk_equality(self) -> None:
        def mutate_contract(bundle):
            bundle.contract["tokenizer"]["type"] = "substituted"

        def mutate_audit(bundle):
            bundle.audit["tokenizer"]["type"] = "substituted"
            bundle.audit["sha256"] = canonical_sha256({
                key: value for key, value in bundle.audit.items() if key != "sha256"
            })

        def mutate_profile(bundle):
            bundle.profile["semantics"]["execution_domain"] = "substituted"
            bundle.profile["profile_sha256"] = canonical_sha256({
                key: value for key, value in bundle.profile.items() if key != "profile_sha256"
            })

        def mutate_certificate(bundle):
            bundle.certificate["materialization"]["all_package_parameters_fit_signed_int32"] = False
            bundle.certificate["certificate_sha256"] = canonical_sha256({
                key: value for key, value in bundle.certificate.items()
                if key != "certificate_sha256"
            })

        def mutate_manifest(bundle):
            bundle.manifest["max_context"] = 31

        def mutate_oracle(bundle):
            bundle.oracle["identity"]["reference_revision"] = "0" * 40
            bundle.oracle["oracle_sha256"] = canonical_sha256({
                key: value for key, value in bundle.oracle.items() if key != "oracle_sha256"
            })

        def mutate_oracle_logits(bundle):
            bundle.oracle_logits = bundle.oracle_logits.clone()
            bundle.oracle_logits[123] += 1

        def mutate_receipt(bundle):
            bundle.receipt["execution"]["domain"] = "substituted"
            bundle.receipt["receipt_sha256"] = canonical_sha256({
                key: value for key, value in bundle.receipt.items() if key != "receipt_sha256"
            })

        cases = (
            ("live_contract_mismatch", mutate_contract),
            ("live_audit_mismatch", mutate_audit),
            ("live_profile_mismatch", mutate_profile),
            ("live_certificate_mismatch", mutate_certificate),
            ("live_manifest_mismatch", mutate_manifest),
            ("live_oracle_mismatch", mutate_oracle),
            ("live_oracle_logits_mismatch", mutate_oracle_logits),
            ("task_2_receipt_mismatch", mutate_receipt),
        )
        for code, mutate in cases:
            with self.subTest(code=code):
                changed = self.verifier._fresh_bundle(self.bundle)
                mutate(changed)
                with self.assertRaisesRegex(ValueError, code):
                    self.verifier._authenticate_authority(changed, ROOT)

    def test_authority_records_only_validated_disk_component_hashes(self) -> None:
        authority = self.verifier._authenticate_authority(self.bundle, ROOT)

        self.assertEqual(set(authority["live_components"]), {
            "contract", "audit", "fixed_profile", "reachable_certificate", "manifest",
            "independent_oracle", "oracle_logits", "receipt", "model_state",
        })
        for component in authority["live_components"].values():
            self.assertRegex(component["canonical_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(component["status"], "matched_authenticated_authority")
        model_state = authority["live_components"]["model_state"]["state_binding"]
        self.assertEqual(model_state["normalization"], "none")
        self.assertEqual(
            model_state["task_2_named_state_sha256"], authority["task_2"]["model_state_sha256"]
        )
        self.assertGreater(model_state["tensor_count"], 0)

    def test_export_state_binding_strips_only_the_proven_model_prefix(self) -> None:
        wrapped = {
            "model.weight": torch.tensor([[1, -2], [3, 4]], dtype=torch.int64),
            "model.bias": torch.tensor([5, 6], dtype=torch.int64),
        }
        normalized = {name.removeprefix("model."): value for name, value in wrapped.items()}

        binding = self.verifier._canonical_state_binding(wrapped, wrapper_prefix="model.")

        self.assertEqual(binding["normalization"], "removed_exact_prefix:model.")
        self.assertEqual([item["name"] for item in binding["tensors"]], ["bias", "weight"])
        self.assertEqual(binding["tensor_count"], 2)
        unwrapped = self.verifier._canonical_state_binding(normalized)
        self.assertEqual(
            binding["normalized_tensors_sha256"], unwrapped["normalized_tensors_sha256"]
        )
        self.assertEqual(
            binding["task_2_named_state_sha256"], independent_named_state_sha256(normalized)
        )
        self.assertEqual(binding["canonical_sha256"], canonical_sha256({
            "normalization": "removed_exact_prefix:model.",
            "tensors": binding["tensors"],
        }))
        changed = copy.deepcopy(wrapped)
        changed["model.weight"][0, 0] += 1
        self.assertNotEqual(
            self.verifier._canonical_state_binding(changed, wrapper_prefix="model.")
            ["canonical_sha256"],
            binding["canonical_sha256"],
        )
        with self.assertRaisesRegex(ValueError, "export_state_name_mismatch"):
            self.verifier._canonical_state_binding(
                {"unproven.weight": wrapped["model.weight"]}, wrapper_prefix="model."
            )


class TinyStories1MExactGenerationArtifactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = load_script(VERIFIER)
        cls.artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    @staticmethod
    def _rehash_later_evidence(artifact: dict[str, object], index: int) -> None:
        generation = artifact["generation"]
        canonical_steps = generation["canonical_steps"]
        canonical = canonical_steps[index]
        canonical["evidence_sha256"] = canonical_sha256(canonical["evidence"])
        canonical["step_sha256"] = canonical_sha256({
            key: value for key, value in canonical.items() if key != "step_sha256"
        })
        for run in generation["runs"]:
            step = run["steps"][index]
            step["eager_evidence_sha256"] = canonical["evidence_sha256"]
            step["exported_evidence_sha256"] = canonical["evidence_sha256"]
            step["comparison_sha256"] = canonical_sha256({
                key: value for key, value in step.items() if key != "comparison_sha256"
            })
            for mode in ("eager", "exported"):
                mode_value = run["modes"][mode]
                mode_value["step_evidence_sha256"] = [
                    item[f"{mode}_evidence_sha256"] for item in run["steps"]
                ]
                mode_value["aggregate_sha256"] = canonical_sha256({
                    key: value for key, value in mode_value.items()
                    if key != "aggregate_sha256"
                })
            transcript_payload = {
                "prompt_ids": generation["prompt_ids"],
                "tokens": run["tokens"],
                "canonical_step_sha256": [item["step_sha256"] for item in canonical_steps],
                "step_comparison_sha256": [
                    item["comparison_sha256"] for item in run["steps"]
                ],
                "mode_aggregate_sha256": {
                    mode: run["modes"][mode]["aggregate_sha256"]
                    for mode in ("eager", "exported")
                },
                "model_receipt_sha256": run["model_receipt_sha256"],
            }
            run["transcript_sha256"] = canonical_sha256(transcript_payload)
            run["run_sha256"] = canonical_sha256({
                key: value for key, value in run.items() if key != "run_sha256"
            })
        transcripts = [run["transcript_sha256"] for run in generation["runs"]]
        generation["determinism"]["run_transcript_sha256"] = transcripts
        generation["determinism"]["all_transcripts_identical"] = len(set(transcripts)) == 1
        generation["determinism"]["three_run_sha256"] = canonical_sha256(transcripts)
        generation["result_sha256"] = canonical_sha256({
            key: value for key, value in generation.items() if key != "result_sha256"
        })
        artifact["artifact_sha256"] = canonical_sha256({
            key: value for key, value in artifact.items() if key != "artifact_sha256"
        })

    def test_artifact_integrity_is_offline_and_independently_rederived(self) -> None:
        artifact = self.artifact
        generation = artifact["generation"]
        self.assertLess(ARTIFACT.stat().st_size, 2_000_000)
        self.assertEqual(artifact["schema"], "tinystories-1m-exact-generation-v2")
        self.assertEqual(artifact["identity"]["task_1"]["commit"], TASK_1_COMMIT)
        self.assertEqual(artifact["identity"]["task_2"]["commit"], TASK_2_COMMIT)
        self.assertEqual(artifact["identity"]["sources"], {
            "scripts/comparison/verify_tinystories_1m_exact_generation.py":
                hashlib.sha256(VERIFIER.read_bytes()).hexdigest(),
            "tests/test_tinystories_1m_exact_generation.py":
                hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        })
        self.assertEqual(artifact["artifact_sha256"], canonical_sha256({
            key: value for key, value in artifact.items() if key != "artifact_sha256"
        }))
        evidence_hashes = []
        for index, step in enumerate(generation["canonical_steps"]):
            self.assertEqual(step["evidence_sha256"], canonical_sha256(step["evidence"]))
            self.assertEqual(step["step_sha256"], canonical_sha256({
                key: value for key, value in step.items() if key != "step_sha256"
            }))
            self.assertNotIn("eager", step)
            self.assertNotIn("exported", step)
            evidence_hashes.append(step["evidence_sha256"])
            self.assertEqual(index, step["step_index"])
        for run in generation["runs"]:
            for mode in ("eager", "exported"):
                summary = run["modes"][mode]
                payload = {key: value for key, value in summary.items()
                           if key != "aggregate_sha256"}
                self.assertEqual(summary["aggregate_sha256"], canonical_sha256(payload))
                self.assertEqual(summary["step_evidence_sha256"], evidence_hashes)
                self.assertEqual(summary["tensor_observation_count"], 16 * 386)
            self.assertEqual(run["tensor_comparison_count"], 16 * 386)
            for step in run["steps"]:
                self.assertNotIn("dual_evidence", step)
                self.assertEqual(step["comparison_sha256"], canonical_sha256({
                    key: value for key, value in step.items() if key != "comparison_sha256"
                }))
            self.assertEqual(run["run_sha256"], canonical_sha256({
                key: value for key, value in run.items() if key != "run_sha256"
            }))
        self.assertEqual(generation["result_sha256"], canonical_sha256({
            key: value for key, value in generation.items() if key != "result_sha256"
        }))
        self.verifier.validate_artifact(artifact, ROOT)

    def test_changed_later_observation_changes_aggregate_and_fails_closed(self) -> None:
        changed = copy.deepcopy(self.artifact)
        before = changed["generation"]["runs"][0]["modes"]["eager"]["aggregate_sha256"]
        changed["generation"]["canonical_steps"][14]["evidence"]["logits_sha256"] = "0" * 64
        self._rehash_later_evidence(changed, 14)
        after = changed["generation"]["runs"][0]["modes"]["eager"]["aggregate_sha256"]

        self.assertNotEqual(before, after)
        with self.assertRaisesRegex(ValueError, "artifact_evidence_mismatch"):
            self.verifier.validate_artifact(changed, ROOT)

    def test_self_rehashed_identity_and_export_state_drift_fail_closed(self) -> None:
        mutations = (
            lambda value: value["identity"]["task_1"].__setitem__("commit", "0" * 40),
            lambda value: value["identity"]["sources"].__setitem__(
                "tests/test_tinystories_1m_exact_generation.py", "0" * 64
            ),
            lambda value: value["generation"]["export_cache"]["programs"]["19"]
                ["normalized_state_binding"].__setitem__("normalization", "unproven"),
        )
        for mutate in mutations:
            with self.subTest(mutation=repr(mutate)):
                changed = copy.deepcopy(self.artifact)
                mutate(changed)
                if changed["generation"] != self.artifact["generation"]:
                    programs = changed["generation"]["export_cache"]["programs"]
                    changed["generation"]["export_cache"]["cache_sha256"] = canonical_sha256(
                        programs
                    )
                    changed["generation"]["result_sha256"] = canonical_sha256({
                        key: value for key, value in changed["generation"].items()
                        if key != "result_sha256"
                    })
                changed["artifact_sha256"] = canonical_sha256({
                    key: value for key, value in changed.items() if key != "artifact_sha256"
                })
                with self.assertRaisesRegex(ValueError, "artifact_evidence_mismatch"):
                    self.verifier.validate_artifact(changed, ROOT)


@unittest.skipUnless(PACKAGE.is_dir() and MODEL.is_dir(), "frozen package/model inputs unavailable")
class TinyStories1MExactGenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = load_script(VERIFIER)
        cls.bundle = load_exact_model(CONTRACT, PACKAGE, MODEL)
        cls.result = cls.verifier.verify_generation(
            cls.bundle, [7454, 2402, 257, 640], EXPECTED, count=3
        )

    def test_three_runs_match_the_frozen_sequence(self) -> None:
        result = self.result

        self.assertEqual(result["status"], "matched")
        self.assertEqual([run["tokens"] for run in result["runs"]], [EXPECTED] * 3)
        artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        self.assertEqual(artifact, self.verifier.build_artifact(ROOT, result))
        self.verifier.validate_artifact(artifact, ROOT, result)

    def test_every_context_replays_eager_and_exported_observations(self) -> None:
        self.assertEqual(self.result["context_lengths"], list(range(4, 20)))
        self.assertEqual(self.result["export_cache"]["export_count"], 16)
        self.assertEqual(self.result["export_cache"]["replay_count"], 48)
        self.assertEqual(
            sorted(map(int, self.result["export_cache"]["programs"])), list(range(4, 20))
        )
        for canonical in self.result["canonical_steps"]:
            self.assertEqual(len(canonical["evidence"]["checkpoint_sha256"]), 12)
            self.assertEqual(len(canonical["evidence"]["qdq_boundary_sha256"]), 97)
            self.assertEqual(len(canonical["evidence"]["gemv_accumulator_sha256"]), 49)
            self.assertEqual(len(canonical["evidence"]["nonlinear_boundary_sha256"]), 33)
        for program in self.result["export_cache"]["programs"].values():
            self.assertEqual(
                program["normalized_state_binding"]["authority_ref"],
                "identity.live_components.model_state.state_binding",
            )
        for run in self.result["runs"]:
            self.assertEqual(len(run["steps"]), 16)
            for length, step in zip(range(4, 20), run["steps"], strict=True):
                with self.subTest(run=run["run_index"], length=length):
                    self.assertEqual(step["context_length"], length)
                    self.assertEqual(step["comparison_status"], "matched")
                    self.assertEqual(step["eager_evidence_sha256"],
                                     step["exported_evidence_sha256"])
                    self.assertEqual(step["tensor_comparison_count"], 386)
                    self.assertNotIn("dual_evidence", step)

    def test_fresh_runs_have_identical_authenticated_transcripts(self) -> None:
        runs = self.result["runs"]
        self.assertEqual([run["bundle_instance"] for run in runs], [1, 2, 3])
        self.assertEqual(len({run["transcript_sha256"] for run in runs}), 1)
        self.assertEqual(
            len({run["model_state_binding"]["normalized_tensors_sha256"] for run in runs}), 1
        )
        self.assertEqual(
            self.result["determinism"]["run_transcript_sha256"],
            [run["transcript_sha256"] for run in runs],
        )
        self.assertRegex(self.result["determinism"]["three_run_sha256"], r"^[0-9a-f]{64}$")

    def test_tie_breaking_selects_the_smallest_token_id(self) -> None:
        selected, tie_count = self.verifier.select_greedy_token([7, 9, 9, 4])
        self.assertEqual((selected, tie_count), (1, 2))

    def test_inputs_fail_closed_before_generation(self) -> None:
        cases = (
            ("prompt_identity_mismatch", [7454, 2402, 257], EXPECTED, 3),
            ("expected_tokens_identity_mismatch", [7454, 2402, 257, 640], EXPECTED[:-1] + [0], 3),
            ("generation_count_mismatch", [7454, 2402, 257, 640], EXPECTED, 2),
        )
        for code, prompt, expected, count in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                self.verifier.verify_generation(self.bundle, prompt, expected, count=count)

if __name__ == "__main__":
    unittest.main()
