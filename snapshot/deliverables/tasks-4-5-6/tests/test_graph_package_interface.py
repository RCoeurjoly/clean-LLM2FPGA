"""Evaluate public Nix targets; historical reproduction has explicit names."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("nix"), "requires native Nix evaluation")
class PackageInterfaceTests(unittest.TestCase):
    def test_stable_scratch_target_selects_exact_onchip_policy(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['nix', 'eval', '--json', '.#graph-core-1m-stable-scratch.profile'],
            cwd=root, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'memory_implementation': 'bram', 'narrow_immutable': True,
            'offchip_immutable': False, 'recover_restoring_sqrt_loops': True,
            'coalesce_pointwise_pingpong': True, 'fuse_gemv_epilogues': True,
            'pool_private_capacity': True, 'stable_scratch_pooling': True,
            'lut_memory_max_bits':0})

    def test_lut_memory_override_records_threshold_and_rejects_boolean(self):
        root=Path(__file__).resolve().parents[1]
        argv=['nix','eval','--json','.#graph-core-1m-stable-scratch','--apply']
        result=subprocess.run(argv+['p: (p.override { lutMemoryMaxBits = 4096; }).profile'],
            cwd=root,text=True,capture_output=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stderr)
        policy=json.loads(result.stdout)
        self.assertEqual(policy['lut_memory_max_bits'],4096)
        self.assertTrue(policy['stable_scratch_pooling'])
        rejected=subprocess.run(argv+['p: (p.override { lutMemoryMaxBits = true; }).profile'],
            cwd=root,text=True,capture_output=True,timeout=120)
        self.assertNotEqual(rejected.returncode,0)

    def test_legacy_reproduction_is_explicitly_historical(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["nix", "eval", "--json", ".#packages.x86_64-linux", "--apply", "builtins.attrNames"],
            cwd=root, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = json.loads(result.stdout)
        legacy = [name for name in names if name.startswith(("task4-board-evidence", "task5-tinystories-", "task6-bram-"))]
        self.assertEqual(legacy, [], "legacy implementations exposed as current task targets")
        for name in ("historical-task6-bram-bitstream", "historical-task4-board-evidence",
                     "historical-task5-tinystories-family-stage-matrix", "graph-core-1m-ddr",
                     "graph-core-1m-ddr-transport"):
            self.assertIn(name, names)

    def test_tasks4_and6_share_authenticated_graph_derivations(self):
        root = Path(__file__).resolve().parents[1]
        expression = "p: { core4 = p.task4-graph-core.drvPath; core6 = p.task6-graph-core.drvPath; core = p.graph-core-1m-ddr.drvPath; transport4 = p.task4-graph-ddr-transport.drvPath; transport6 = p.task6-graph-ddr-transport.drvPath; transport = p.graph-core-1m-ddr-transport.drvPath; coarse6 = p.task6-graph-xc7-coarse.drvPath; coarse = p.graph-core-1m-ddr-xc7-coarse.drvPath; coarseInput = p.task6-graph-xc7-coarse.synthesis.drvPath; synthesis = p.graph-core-1m-ddr-synthesis-export.drvPath; }"
        result = subprocess.run(
            ["nix", "eval", "--json", ".#packages.x86_64-linux", "--apply", expression],
            cwd=root, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        self.assertEqual(paths["core4"], paths["core"])
        self.assertEqual(paths["core6"], paths["core"])
        self.assertEqual(paths["transport4"], paths["transport"])
        self.assertEqual(paths["transport6"], paths["transport"])
        self.assertEqual(paths["coarse6"], paths["coarse"])
        self.assertEqual(paths["coarseInput"], paths["synthesis"])

    def test_ram_mapping_consumes_the_graph_coarse_checkpoint(self):
        root = Path(__file__).resolve().parents[1]
        expression = "p: { ram = p.graph-core-1m-ddr-xc7-ram.drvPath; task = p.task6-graph-xc7-ram.drvPath; parent = p.graph-core-1m-ddr-xc7-ram.coarse.drvPath; coarse = p.graph-core-1m-ddr-xc7-coarse.drvPath; }"
        result = subprocess.run(
            ["nix", "eval", "--json", ".#packages.x86_64-linux", "--apply", expression],
            cwd=root, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        self.assertEqual(paths["ram"], paths["task"])
        self.assertEqual(paths["parent"], paths["coarse"])

    def test_final_mapping_consumes_the_authenticated_graph_ram_stage(self):
        root = Path(__file__).resolve().parents[1]
        expression = "p: { final = p.graph-core-1m-ddr-xc7-final.drvPath; task = p.task6-graph-xc7-final.drvPath; parent = p.graph-core-1m-ddr-xc7-final.ram.drvPath; ram = p.graph-core-1m-ddr-xc7-ram.drvPath; }"
        result = subprocess.run(
            ["nix", "eval", "--json", ".#packages.x86_64-linux", "--apply", expression],
            cwd=root, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = json.loads(result.stdout)
        self.assertEqual(paths["final"], paths["task"])
        self.assertEqual(paths["parent"], paths["ram"])
