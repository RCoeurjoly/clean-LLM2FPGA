"""The numerical gate must authenticate the exact synthesis RTL and its core."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt, _identity


class SynthesisNumericalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.core = self.root/'core'; self.core.mkdir()
        self.synthesis = self.root/'synthesis'
        self.core_doc = {'identity_sha256': 'core-id', 'profile_sha256': 'profile-id'}
        (self.core/'core.json').write_text(json.dumps(self.core_doc))
        self.image = self.core/'weights.hex'; self.image.write_text('01\n80\n')
        python = Path(sys.executable).resolve()
        run_stage(StageSpec('sv', (str(python), '-c',
            "from pathlib import Path;Path('kernel.sv').write_text('module main;endmodule')"),
            {str(self.image): digest(self.image)}, {str(python): digest(python)},
            {}, ('kernel.sv',)), self.synthesis/'sv')
        self.rtl = self.synthesis/'sv/kernel.sv'
        dependencies = verify_stage_receipt(self.synthesis/'sv')
        dependencies[str(self.core/'core.json')] = digest(self.core/'core.json')
        self.export = {'schema_version': 1, 'core_identity_sha256': 'core-id',
            'profile_sha256': 'profile-id', 'output': str(self.rtl),
            'dependency_hashes': dependencies}
        self.write_export()
        script = Path(__file__).resolve().parents[1]/'scripts/comparison/verify_graph_generation_rtl.py'
        spec = importlib.util.spec_from_file_location('synthesis_numerical_gate', script)
        self.driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.driver)

    def write_export(self):
        self.export['identity_sha256'] = _identity(
            {k:v for k,v in self.export.items() if k != 'identity_sha256'})
        (self.synthesis/'core-synthesis.json').write_text(json.dumps(self.export))

    def test_selects_authenticated_synthesis_rtl(self):
        rtl, identity = self.driver.authenticated_rtl(self.core, self.synthesis)
        self.assertEqual(rtl, self.rtl)
        self.assertEqual(identity, self.export['identity_sha256'])

    def test_rejects_export_bound_to_another_core(self):
        self.export['core_identity_sha256'] = 'other-core'; self.write_export()
        with self.assertRaisesRegex(ValueError, 'core'):
            self.driver.authenticated_rtl(self.core, self.synthesis)

    def test_rejects_changed_initializer(self):
        self.image.write_text('00\n00\n')
        with self.assertRaisesRegex(ValueError, 'dependency'):
            self.driver.authenticated_rtl(self.core, self.synthesis)

    def test_rejects_an_aggregate_receipt_pointing_away_from_native_output(self):
        self.export['output'] = str(self.root/'foreign.sv'); self.write_export()
        with self.assertRaisesRegex(ValueError, 'output'):
            self.driver.authenticated_rtl(self.core, self.synthesis)
