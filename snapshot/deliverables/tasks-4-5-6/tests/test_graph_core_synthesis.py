"""Production synthesis export must consume an authenticated common core."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts.pipeline.export_graph_core_synthesis import export_core_synthesis
from compiler.pipeline import CompilationResult, CompilerTool
from compiler.stages import StageSpec, digest, run_stage


class CoreSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.core = self.root/'core'; self.core.mkdir()
        self.output = self.root/'output'; self.output.mkdir()
        (self.core/'core.json').write_text('authenticated core fixture')
        self.source = self.core/'initialized/futil/kernel.futil'
        self.source.parent.mkdir(parents=True)
        # A fake native CLI emits a fixture; full native export is a separate gate.
        self.source.write_text("from pathlib import Path;Path('kernel.sv').write_text('native fixture')")
        self.interface = self.core/'initialized/external-interface/interface.json'
        self.interface.parent.mkdir(parents=True)
        self.interface.write_text('{"memories": []}')
        self.image = self.core/'weights.hex'; self.image.write_text('01\n80\n')
        self.receipt = {'identity_sha256': 'core-identity', 'dependency_hashes':
                        {str(self.core/'core.json'): digest(self.core/'core.json'),
                         str(self.interface): digest(self.interface),
                         str(self.image): digest(self.image)}}
        library = self.root/'library'; library.mkdir()
        (library/'fixture.futil').write_text('library fixture')
        self.profile = type('Profile', (), {'tools': {'calyx': CompilerTool(Path(sys.executable).resolve())}, 'calyx_library': library,
                                            'identity_sha256': 'profile-identity'})()
        self.calls = []

    def bind(self, source, manifest, primitives, output, tools, **kwargs):
        self.calls.append((source, manifest, primitives, kwargs))
        executable = Path(sys.executable).resolve()
        stage = output/'sv'
        receipt = run_stage(StageSpec('sv', (str(executable), '-c',
            "from pathlib import Path;Path('kernel.sv').write_text('native fixture')"),
            {str(source): digest(source)}, {str(executable): digest(executable)}, {},
            ('kernel.sv',)), stage)
        return CompilationResult('fixture', stage/'kernel.sv', (receipt,))

    def run_export(self):
        with patch('scripts.pipeline.export_graph_core_synthesis.verify_core_receipt',
                   return_value=self.receipt), patch(
                'scripts.pipeline.export_graph_core_synthesis._verified_artifact',
                return_value=(self.source, {str(self.source): digest(self.source)})) as artifact, patch(
                'scripts.pipeline.export_graph_core_synthesis.bind_initialized_memories',
                side_effect=self.bind, create=True):
            result = export_core_synthesis(self.core, self.profile, self.output)
            self.artifact_arguments = artifact.call_args.args
            return result

    def test_native_synthesis_consumes_core_and_authenticates_dependencies(self):
        result = self.run_export()
        self.assertEqual(result['core_identity_sha256'], 'core-identity')
        self.assertEqual(self.calls, [], 'synthesis must reuse the authenticated bound core')
        self.assertEqual(self.artifact_arguments,
                         (self.core/'initialized/futil', ('futil',), 'kernel.futil'))
        spec = json.loads((self.output/'sv/spec.json').read_text())
        self.assertEqual(spec['argv'][1], str(self.source))
        self.assertIn('--synthesis', spec['argv'])
        self.assertNotIn('--nested', spec['argv'], 'native flat mode bounds expression depth')
        self.assertEqual(spec['input_hashes'][str(self.image)], digest(self.image),
                         'downstream mapping must receive initializer identities')
        self.assertEqual(result['external_interface'], str(self.interface))
        self.assertIn(str(self.source), result['dependency_hashes'])
        self.assertIn(str(self.core/'core.json'), result['dependency_hashes'])
        self.assertEqual(json.loads((self.output/'core-synthesis.json').read_text()), result)

    def test_invalid_core_removes_stale_acceptance_before_emission(self):
        stale = self.output/'core-synthesis.json'; stale.write_text('old success')
        with patch('scripts.pipeline.export_graph_core_synthesis.verify_core_receipt',
                   side_effect=ValueError('core changed')), patch(
                'scripts.pipeline.export_graph_core_synthesis.bind_initialized_memories', create=True) as bind:
            with self.assertRaisesRegex(ValueError, 'core changed'):
                export_core_synthesis(self.core, self.profile, self.output)
            bind.assert_not_called()
        self.assertFalse(stale.exists())

    def test_core_changes_during_export_cannot_leave_success(self):
        with patch('scripts.pipeline.export_graph_core_synthesis.verify_core_receipt',
                   side_effect=[self.receipt, ValueError('core changed')]), patch(
                'scripts.pipeline.export_graph_core_synthesis._verified_artifact',
                return_value=(self.source, {})), patch(
                'scripts.pipeline.export_graph_core_synthesis.bind_initialized_memories',
                side_effect=self.bind, create=True):
            with self.assertRaisesRegex(ValueError, 'core changed'):
                export_core_synthesis(self.core, self.profile, self.output)
        self.assertFalse((self.output/'core-synthesis.json').exists())
