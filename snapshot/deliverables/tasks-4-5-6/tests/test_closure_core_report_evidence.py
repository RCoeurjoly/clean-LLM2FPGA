import importlib
import json
from pathlib import Path
import tempfile
import unittest
from compiler.build_profile import CoreBuildProfile
from compiler.pipeline import CompilerTool


class CoreReportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps({'schema_version': 1, 'arithmetic': {
            'profile': 'tinystories-1m-integer-v1',
            'value_fraction_bits': 16, 'scale_fraction_bits': 24}}))
        library = self.root / 'library'
        library.mkdir()
        (library / 'primitive.futil').write_text('primitive')
        tools = {}
        for role in ('torch', 'circt', 'memory', 'translate', 'calyx'):
            file = self.root / role
            file.write_text('declared tool ' + role)
            tools[role] = CompilerTool(file)
        self.profile = CoreBuildProfile(contract, tools, library)

    def collect(self, core, profile):
        module = importlib.import_module('scripts.pipeline.build_closure_measurement_report')
        function = getattr(module, 'load_core_evidence', None)
        self.assertTrue(callable(function), 'authenticated core evidence collection required')
        return function(core, profile)

    def test_untyped_profile_cannot_authorize_report_row(self):
        with self.assertRaisesRegex(ValueError, 'typed'):
            self.collect(self.root / 'absent', object())

    def test_missing_core_receipt_is_not_reported_as_compiled(self):
        with self.assertRaisesRegex(ValueError, 'core evidence'):
            self.collect(self.root / 'absent', self.profile)

    def test_changed_profile_dependency_cannot_authorize_report_row(self):
        (self.root / 'memory').write_text('changed native tool')
        with self.assertRaisesRegex(ValueError, 'dependency changed'):
            self.collect(self.root / 'absent', self.profile)


    def test_active_registered_contract_and_external_contract_share_program(self):
        from scripts.pipeline.build_closure_measurement_report import pipeline_identity
        registered = Path(__file__).resolve().parents[1] / 'compiler/contracts/tinystories-1m-fixed-point.json'
        profile = CoreBuildProfile(registered, self.profile.tools, self.profile.calyx_library)
        self.assertEqual(pipeline_identity(profile), pipeline_identity(self.profile),
                         'registered numerical contract role must not change compiler program')

    def test_contract_policy_fields_outside_inputs_are_preserved(self):
        from scripts.pipeline.build_closure_measurement_report import pipeline_identity
        original = pipeline_identity(self.profile)
        contract = self.root / 'other-contract.json'
        value = json.loads(self.profile.numerical_contract.read_text())
        value['rounding_policy'] = 'different-policy'
        contract.write_text(json.dumps(value))
        profile = CoreBuildProfile(contract, self.profile.tools, self.profile.calyx_library)
        self.assertNotEqual(original, pipeline_identity(profile),
                            'non-input policy fields must remain part of program identity')
