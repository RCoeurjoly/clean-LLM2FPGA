"""A hashed file is not proof that an intermediate compiler stage succeeded."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from compiler.build_core import verify_core_receipt
from compiler.build_profile import CoreBuildProfile
from compiler.pipeline import CompilerTool
from compiler.stages import StageSpec, _identity, digest, run_stage


class IntermediateReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        contract = self.root / 'contract.json'
        contract.write_text('{}')
        library = self.root / 'library'
        library.mkdir()
        (library / 'primitive.futil').write_text('primitive')
        tool = Path(shutil.which('cp')).resolve()
        tools = {name: CompilerTool(tool) for name in ('torch', 'circt', 'memory', 'translate', 'calyx')}
        self.profile = CoreBuildProfile(contract, tools, library)
        source = self.root / 'input.mlir'
        source.write_text('opaque stage-gate fixture; no model computation')
        self.stage = self.root / 'loops'
        spec = StageSpec('loops', (str(tool), str(source), 'loops.mlir'),
                         {str(source): digest(source)}, {str(tool): digest(tool)},
                         {'fixture': 'receipt gate only'}, ('loops.mlir',))
        run_stage(spec, self.stage)

    def write_core(self, omitted=()):
        dependencies = {str(path): digest(path) for path in (
            self.stage / 'spec.json', self.stage / 'success.json', self.stage / 'loops.mlir')
            if path.name not in omitted}
        spec = json.loads((self.stage / 'spec.json').read_text())
        dependencies.update(spec['input_hashes'])
        dependencies.update(spec['tool_hashes'])
        body = {'schema_version': 1, 'profile_sha256': self.profile.identity_sha256,
                'materialized_inputs_sha256': '0' * 64,
                'output': str(self.stage / 'loops.mlir'),
                'graphs': [{'context': length} for length in range(4, 20)],
                'dependency_hashes': dependencies}
        (self.root / 'core.json').write_text(json.dumps({**body, 'identity_sha256': _identity(body)}))

    def test_valid_declared_stage_is_authenticated(self):
        self.write_core()
        verify_core_receipt(self.root, self.profile)

    def test_failed_stage_cannot_be_promoted_by_hashing_its_receipt(self):
        path = self.stage / 'success.json'
        receipt = json.loads(path.read_text())
        receipt['success'] = False
        path.write_text(json.dumps(receipt))
        self.write_core()
        with self.assertRaisesRegex(ValueError, 'stage'):
            verify_core_receipt(self.root, self.profile)

    def test_missing_stage_spec_dependency_is_rejected(self):
        self.write_core(omitted=('spec.json',))
        with self.assertRaisesRegex(ValueError, 'stage'):
            verify_core_receipt(self.root, self.profile)

    def test_missing_final_stage_receipt_is_rejected(self):
        self.write_core(omitted=('success.json',))
        with self.assertRaisesRegex(ValueError, 'stage'):
            verify_core_receipt(self.root, self.profile)
