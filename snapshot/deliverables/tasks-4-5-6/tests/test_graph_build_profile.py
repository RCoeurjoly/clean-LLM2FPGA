"""A build profile cannot certify changed compiler inputs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from compiler.pipeline import CompilerTool


class BuildProfileTests(unittest.TestCase):
    def test_lut_placement_policy_is_authenticated_validated_and_roundtrips(self):
        from dataclasses import replace
        from compiler.build_profile import CoreBuildProfile
        from scripts.pipeline.export_graph_ddr_transport import read_profile
        import json
        self.assertIn('lut_memory_max_bits',CoreBuildProfile.__dataclass_fields__)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=self.profile(root)
            selected=replace(base,lut_memory_max_bits=4096)
            self.assertEqual(selected.document()['memory_policy']['lut_memory_max_bits'],4096)
            self.assertNotEqual(base.identity_sha256,selected.identity_sha256)
            (root/'profile.json').write_text(json.dumps(selected.document()))
            restored=read_profile(root,selected.numerical_contract,selected.calyx_library)
            self.assertEqual(restored.identity_sha256,selected.identity_sha256)
            for value in (-1,True,1.0,None,'4096'):
                with self.subTest(value=value),self.assertRaisesRegex(ValueError,'LUT memory'):
                    replace(base,lut_memory_max_bits=value)
            with self.assertRaisesRegex(ValueError,'BRAM'):
                replace(base,memory_implementation='register',lut_memory_max_bits=1)

    def test_lut_placement_cli_preserves_threshold_and_rejects_invalid_policy(self):
        from compiler.compile_core import parse_arguments
        args=[]
        for name in ('snapshot','package','numerical-contract','output','calyx-library',
                     'torch-tool','circt-tool','memory-tool','translate-tool','calyx-tool'):
            args+=['--'+name,'/declared/'+name]
        self.assertEqual(parse_arguments(args+['--lut-memory-max-bits','4096']).lut_memory_max_bits,4096)
        self.assertEqual(parse_arguments(args).lut_memory_max_bits,0)
        for extra in (['--lut-memory-max-bits','-1'],['--lut-memory-max-bits','1.5'],
                      ['--memory-implementation','lutram','--lut-memory-max-bits','1']):
            with self.subTest(extra=extra),self.assertRaises(SystemExit):
                parse_arguments(args+extra)

    def profile(self, root):
        self.assertIsNotNone(importlib.util.find_spec('compiler.build_profile'),
                             'common authenticated build profile is missing')
        from compiler.build_profile import CoreBuildProfile
        library = root / 'library'
        library.mkdir()
        (library / 'primitives.futil').write_text('primitive source\n')
        contract = root / 'contract.json'
        contract.write_text('{"schema_version":1}\n')
        tools = {}
        for name in ('torch', 'circt', 'memory', 'translate', 'calyx'):
            path = root / name
            path.write_text(name + '\n')
            tools[name] = CompilerTool(path)
        return CoreBuildProfile(contract, tools, library)

    def test_stable_pooling_policy_is_authenticated_and_roundtrips(self):
        from dataclasses import replace
        from compiler.build_profile import CoreBuildProfile
        import json
        from scripts.pipeline.export_graph_ddr_transport import read_profile
        self.assertIn('stable_scratch_pooling', CoreBuildProfile.__dataclass_fields__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = self.profile(root)
            base = replace(original, pool_private_capacity=True)
            selected = replace(base, stable_scratch_pooling=True)
            doc = selected.document()
            self.assertTrue(doc['optimization_policy']['stable_scratch_pooling'])
            self.assertNotEqual(base.identity_sha256, selected.identity_sha256)
            (root/'profile.json').write_text(json.dumps(doc))
            restored = read_profile(root, selected.numerical_contract, selected.calyx_library)
            self.assertEqual(restored.identity_sha256, selected.identity_sha256)
            with self.assertRaisesRegex(ValueError, 'stable scratch'):
                replace(original, stable_scratch_pooling=True)
            with self.assertRaisesRegex(ValueError, 'stable scratch'):
                replace(base, stable_scratch_pooling=1)

    def test_stable_pooling_cli_requires_capacity_and_preserves_selection(self):
        from compiler.compile_core import parse_arguments
        args = []
        for name in ('snapshot', 'package', 'numerical-contract', 'output', 'calyx-library',
                     'torch-tool', 'circt-tool', 'memory-tool', 'translate-tool', 'calyx-tool'):
            args += ['--'+name, '/declared/'+name]
        selected = parse_arguments(args + ['--pool-private-capacity', '--stable-scratch-pooling'])
        self.assertTrue(selected.stable_scratch_pooling)
        self.assertTrue(selected.pool_private_capacity)
        self.assertFalse(parse_arguments(args).stable_scratch_pooling)
        with self.assertRaises(SystemExit):
            parse_arguments(args + ['--stable-scratch-pooling'])

    def test_offchip_policy_is_authenticated_and_rejects_narrowing(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as temporary:
            original = self.profile(Path(temporary))
            base = replace(original, narrow_immutable=False)
            offchip = replace(base, offchip_immutable=True)
            self.assertTrue(offchip.document()['memory_policy']['offchip_immutable'])
            self.assertEqual(offchip.document()['memory_policy']['ddr_layout'],
                             {'data_width': 576, 'address_width': 25})
            source = Path(__file__).resolve().parents[1] / 'compiler/ddr_images.py'
            self.assertIn(str(source), offchip.document()['dependency_hashes'])
            self.assertNotEqual(base.identity_sha256, offchip.identity_sha256)
            with self.assertRaisesRegex(ValueError, 'off-chip.*narrow'):
                replace(original, offchip_immutable=True)

    def test_changed_tool_invalidates_existing_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            profile.validate()
            (root / 'memory').write_text('different native memory pass\n')
            with self.assertRaisesRegex(ValueError, 'profile dependency changed'):
                profile.validate()

    def test_changed_numerical_contract_invalidates_existing_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            (root / 'contract.json').write_text('{"different_rounding":true}\n')
            with self.assertRaisesRegex(ValueError, 'profile dependency changed'):
                profile.validate()

    def test_changed_primitive_library_invalidates_existing_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = self.profile(root)
            (root / 'library/primitives.futil').write_text('different primitive\n')
            with self.assertRaisesRegex(ValueError, 'profile dependency changed'):
                profile.validate()

    def test_only_complete_generation_domain_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            from compiler.build_profile import CoreBuildProfile
            with self.assertRaisesRegex(ValueError, 'contexts 4 through 19'):
                CoreBuildProfile(profile.numerical_contract, profile.tools,
                                 profile.calyx_library, contexts=(4,))

    def test_owned_memory_templates_are_authenticated_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            root = Path(__file__).resolve().parents[1]
            dependencies = profile.document()['dependency_hashes']
            for name in ('rtl/primitives/closure_memory.sv',
                         'rtl/primitives/closure_narrow_rom.sv',
                         'calyx/primitives/closure_memory.futil'):
                self.assertIn(str(root / name), dependencies)

    def test_profile_records_native_passes_and_rejects_extra_tool_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = self.profile(Path(temporary))
            self.assertEqual(profile.document()['contexts'], list(range(4, 20)))
            self.assertEqual(profile.document()['pass_sequence'][4]['arguments'],
                             ['--verify-each', '--canonicalize', '--scf-for-to-while', '--canonicalize'])
            for stage in profile.document()['pass_sequence']:
                if stage['name'] in ('calyx-ir', 'memory-contracts'):
                    self.assertEqual(stage['output'], 'calyx.mlirbc')
                    self.assertIn('--emit-bytecode', stage['arguments'])
            from compiler.build_profile import CoreBuildProfile
            with self.assertRaisesRegex(ValueError, 'compiler tool roles'):
                CoreBuildProfile(profile.numerical_contract,
                    {**profile.tools, 'legacy-postpass': profile.tools['torch']},
                    profile.calyx_library)
