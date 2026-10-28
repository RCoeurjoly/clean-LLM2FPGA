"""Acceptance semantics for a cross-size family stage matrix."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.analysis import report_tinystories_family_stages as report


REVISION = "a" * 40


def evidence(*, synthesis_status="pass"):
    memories = {"weights": {"width": 8, "elements": 1024}}
    return {
        "compiler": {"size_label": "3M", "revision": REVISION,
                     "source_memories": memories, "sv_bytes": 200},
        "image": {"size_label": "3M", "revision": REVISION,
                  "source_memories": copy.deepcopy(memories),
                  "status": "image_emitted_not_simulated"},
        "simulation": {"status": "matched", "first_token": 11, "cycles": 123},
        "synthesis": {"status": synthesis_status,
                      "resources": ({"LUT": 1, "FF": 2, "BRAM36": 3,
                                     "BRAM18": 4, "DSP": 5}
                                    if synthesis_status == "pass" else None),
                      "reason": None if synthesis_status == "pass" else "deadline"},
    }


class FamilyStageMatrixTests(unittest.TestCase):
    def setUp(self):
        self.inventory = [{"size_label": "3M", "revision": REVISION,
                           "parameters": 8278400, "n_layer": 8, "n_embd": 128}]

    def test_pass_reports_real_resources_but_no_invented_fmax_or_board_result(self):
        row = report.build_matrix(self.inventory, {"3M": evidence()})["models"][0]
        self.assertEqual(row["stages"]["simulation"]["status"], "pass")
        self.assertEqual(row["stages"]["synthesis"]["status"], "pass")
        self.assertEqual(row["resources"]["BRAM36"], 3)
        self.assertEqual(row["image_bits"], 8192)
        self.assertIsNone(row["fmax_mhz"])
        self.assertEqual(row["stages"]["board"]["status"], "not_attempted")

    def test_timed_out_synthesis_has_no_resource_values_or_fit_claim(self):
        row = report.build_matrix(self.inventory,
                                  {"3M": evidence(synthesis_status="timeout")})["models"][0]
        self.assertEqual(row["first_failure"], "synthesis: deadline")
        self.assertIsNone(row["resources"])
        self.assertEqual(row["stages"]["route"]["status"], "skipped")

    def test_each_memory_dimension_requires_a_positive_integer(self):
        for field in ('width', 'elements'):
            for invalid in (True, False, 0, -1, 1.5, '8', None):
                with self.subTest(field=field, value=invalid):
                    item = evidence()
                    item['compiler']['source_memories']['invalid'] = {
                        'width': 8, 'elements': 1024, field: invalid}
                    item['image']['source_memories'] = copy.deepcopy(
                        item['compiler']['source_memories'])
                    with self.assertRaisesRegex(ValueError, 'memory dimensions'):
                        report.build_matrix(self.inventory, {'3M': item})

    def test_missing_or_nonobject_memory_descriptor_is_rejected(self):
        for invalid in ({'width': 8}, {'elements': 1024}, [], None):
            with self.subTest(descriptor=invalid):
                item = evidence()
                item['compiler']['source_memories']['invalid'] = invalid
                item['image']['source_memories'] = copy.deepcopy(
                    item['compiler']['source_memories'])
                with self.assertRaisesRegex(ValueError, 'memory dimensions'):
                    report.build_matrix(self.inventory, {'3M': item})

    def test_mapped_bram_over_capacity_is_screened_without_claiming_board_fit(self):
        capacity = {"resources": {"available": {"RAMB36E1": 2}}}
        row = report.build_matrix(self.inventory, {"3M": evidence()},
                                  board_capacity=capacity)["models"][0]
        self.assertEqual(row["capacity_screen"], {
            "bram36_required": 5, "bram36_available": 2,
            "conclusion": "exceeds_bram36_capacity"})
        self.assertEqual(row["stages"]["board"]["status"], "not_attempted")

    def test_bram18_only_design_can_exceed_bram36_equivalent_capacity(self):
        item = evidence()
        item['synthesis']['resources'].update(BRAM36=0, BRAM18=5)
        capacity = {'resources': {'available': {'RAMB36E1': 2}}}
        row = report.build_matrix(self.inventory, {'3M': item},
                                  board_capacity=capacity)['models'][0]
        self.assertEqual(row['capacity_screen']['bram36_required'], 2.5)
        self.assertEqual(row['capacity_screen']['conclusion'],
                         'exceeds_bram36_capacity')
        self.assertEqual(row['resources']['BRAM18'], 5)
        self.assertEqual(row['resources']['BRAM36'], 0)
        self.assertIsNone(row['fmax_mhz'])

    def test_mismatched_source_image_or_simulation_fails_closed(self):
        item = evidence()
        item["image"]["source_memories"]["weights"]["elements"] = 512
        with self.assertRaisesRegex(ValueError, "source memories"):
            report.build_matrix(self.inventory, {"3M": item})
        item = evidence()
        item["simulation"]["status"] = "mismatched"
        with self.assertRaisesRegex(ValueError, "simulation"):
            report.build_matrix(self.inventory, {"3M": item})

    def test_cli_records_nix_output_and_derivation_for_each_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = {}
            for stage, filename in (("compiler", "build.json"), ("image", "image.json"),
                                    ("simulation", "result.json"), ("synthesis", "result.json")):
                directory = root / stage
                directory.mkdir()
                (directory / filename).write_text(json.dumps(evidence()[stage]))
                inputs[stage] = str(directory)
                inputs[f"{stage}_drv"] = f"/nix/store/{stage}.drv"
            (root / "inventory.json").write_text(json.dumps(self.inventory))
            (root / "inputs.json").write_text(json.dumps({"3M": inputs}))
            subprocess.run([sys.executable, str(Path(report.__file__)),
                            "--inventory", str(root / "inventory.json"),
                            "--inputs", str(root / "inputs.json"),
                            "--output", str(root / "matrix.json"),
                            "--markdown", str(root / "report.md")], check=True)
            matrix = json.loads((root / "matrix.json").read_text())
            self.assertEqual(matrix["models"][0]["resources"]["BRAM36"], 3)
            for source, destination in (("compiler", "compiler_sv"),
                                        ("image", "image"),
                                        ("simulation", "simulation"),
                                        ("synthesis", "synthesis")):
                stage = matrix["models"][0]["stages"][destination]
                self.assertEqual(stage["output"], inputs[source])
                self.assertEqual(stage["derivation"], inputs[f"{source}_drv"])
            self.assertIn("not a board-fit", (root / "report.md").read_text())

    def test_archived_failed_synthesis_has_no_current_nix_identity_or_resources(self):
        inventory = [{**self.inventory[0], "size_label": "28M"}]
        observed = evidence(synthesis_status="failed")
        for stage in ("compiler", "image"):
            observed[stage]["size_label"] = "28M"
        observed.pop("synthesis")
        identities = {"28M": {
            stage: {"output": f"/nix/store/{stage}",
                    "derivation": f"/nix/store/{stage}.drv"}
            for stage in ("compiler", "image", "simulation")
        }}
        archived = {"28M": {"status": "failed", "resources": None,
                             "reason": "std::bad_alloc in BRAM techmapping",
                             "origin": "archived-donor", "donor_commit": "37b2d6c"}}
        row = report.build_matrix(inventory, {"28M": observed}, identities,
                                  archived_synthesis=archived)["models"][0]
        synthesis = row["stages"]["synthesis"]
        self.assertEqual(synthesis["status"], "failed")
        self.assertEqual(synthesis["origin"], "archived-donor")
        self.assertEqual(synthesis["donor_commit"], "37b2d6c")
        self.assertNotIn("derivation", synthesis)
        self.assertNotIn("output", synthesis)
        self.assertIsNone(row["resources"])
        self.assertIn("archived-donor", report.render_report({"models": [row]}))


if __name__ == "__main__":
    unittest.main()
