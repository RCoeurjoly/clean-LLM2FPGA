"""Failed compilation cannot leave an authoritative core receipt."""
import importlib.util
import contextlib
import io
import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from compiler.build_profile import CoreBuildProfile
from compiler.pipeline import CompilerTool
from compiler.stages import verify_stage_receipt


class BuildCoreBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.contract = self.root / 'contract.json'
        self.contract.write_text('{"schema_version":1}\n')
        library = self.root / 'library'
        library.mkdir()
        (library / 'primitive.futil').write_text('primitive\n')
        tools = {}
        for name in ('torch', 'circt', 'memory', 'translate', 'calyx'):
            path = self.root / name
            path.write_text('tool ' + name + '\n')
            tools[name] = CompilerTool(path)
        self.profile = CoreBuildProfile(self.contract, tools, library)
        self.output = self.root / 'core'

    def builder(self):
        self.assertIsNotNone(importlib.util.find_spec('compiler.build_core'),
                             'common graph core builder is missing')
        from compiler.build_core import build_core
        return build_core

    def test_stale_profile_invalidates_previous_core_and_stage_acceptance(self):
        builder = self.builder()
        self.output.mkdir()
        receipt = self.output / 'core.json'
        receipt.write_text(json.dumps({'success': True}))
        layout = self.output / 'ddr/layout.json'
        layout.parent.mkdir()
        layout.write_text('old layout acceptance')
        stage = self.output / 'controller/loops'
        stage.mkdir(parents=True)
        (stage / 'success.json').write_text(json.dumps({'success': True}))
        (self.root / 'memory').write_text('changed native compiler\n')
        with self.assertRaisesRegex(ValueError, 'profile dependency changed'):
            builder(object(), self.profile, self.output)
        self.assertFalse(receipt.exists())
        self.assertFalse(layout.exists())
        self.assertFalse((stage / 'success.json').exists())

    def test_invalid_build_preserves_unowned_verification_receipts(self):
        oracle = self.output / 'verification/success.json'
        oracle.parent.mkdir(parents=True)
        oracle.write_text('verification receipt must stay untouched')
        with self.assertRaisesRegex(ValueError, 'authenticated CompileInputs'):
            self.builder()(object(), self.profile, self.output)
        self.assertTrue(oracle.exists(), 'compiler deleted an unowned verification receipt')
        self.assertEqual(oracle.read_text(), 'verification receipt must stay untouched')

    def test_invalid_inputs_are_rejected_before_capture_or_native_execution(self):
        builder = self.builder()
        with self.assertRaisesRegex(ValueError, 'authenticated CompileInputs'):
            builder(object(), self.profile, self.output)
        self.assertFalse((self.output / 'captures').exists())
        self.assertFalse((self.output / 'core.json').exists())

    def test_invalid_build_clears_all_optional_sharing_stage_acceptance(self):
        stages = ('normalized-index-scales', 'shared-functions', 'reduced-index-scales')
        for name in stages:
            path = self.output/'controller'/name/'success.json'
            path.parent.mkdir(parents=True)
            path.write_text('old selected-policy acceptance')
        with self.assertRaisesRegex(ValueError, 'authenticated CompileInputs'):
            self.builder()(object(), self.profile, self.output)
        for name in stages:
            self.assertFalse((self.output/'controller'/name/'success.json').exists())


class NativeCoreBuildTests(unittest.TestCase):
    def test_offchip_all_contexts_compile_and_cli_preserves_policy(self):
        self.compile_all_contexts(offchip=True)

    def test_all_contexts_compile_through_initialized_native_sv(self):
        self.compile_all_contexts(offchip=False)

    def test_stable_pooling_native_core_and_cli_roundtrip(self):
        self.compile_all_contexts(offchip=False, stable=True)

    def test_lut_placement_native_core_and_cli_roundtrip(self):
        self.compile_all_contexts(offchip=False,stable=True,lut_bits=4096)

    def test_lut_placement_fullwidth_rom_elaborates_and_cli_roundtrips(self):
        self.compile_all_contexts(offchip=False,stable=True,lut_bits=4096,narrow=False)

    def compile_all_contexts(self, *, offchip, stable=False, lut_bits=0, narrow=None):
        self.assertIsNotNone(importlib.util.find_spec('compiler.build_core'),
                             'common graph core builder is missing')
        import torch
        from compiler.build_core import build_core, verify_core_receipt
        from compiler.frontend.package_inputs import load_compile_inputs

        class SmallModel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer('bias', torch.tensor([[0, 3, -1]], dtype=torch.int64))

            def forward(self, tokens):
                return torch.stack((tokens, -tokens, tokens), dim=-1) + self.bias

        names = {'torch': 'LLM2FPGA_TORCH_PASS_TOOL', 'circt': 'LLM2FPGA_CIRCT_TOOL',
                 'memory': 'LLM2FPGA_MEMORY_TOOL', 'translate': 'LLM2FPGA_CALYX_TRANSLATE_TOOL',
                 'calyx': 'LLM2FPGA_CALYX_TOOL'}
        for name in names.values():
            self.assertTrue(name in os.environ, 'pinned native tool missing: ' + name)
        tools = {name: CompilerTool(Path(os.environ[key])) for name, key in names.items()}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot, package = root / 'snapshot', root / 'package'
            snapshot.mkdir()
            package.mkdir()
            (snapshot / 'config.json').write_text('{"model_type":"test"}')
            (snapshot / 'pytorch_model.bin').write_bytes(b'test snapshot')
            (package / 'manifest.json').write_text('{"model":{}}')
            (package / 'weights.bin').write_bytes(b'test weights')
            (package / 'scales.bin').write_bytes(b'test scales')
            identities = {kind: {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in directory.iterdir()}
                          for kind, directory in (('snapshot', snapshot), ('package', package))}
            contract = root / 'contract.json'
            contract.write_text(json.dumps({'schema_version':1, 'inputs':identities, 'arithmetic':{}}))
            inputs = load_compile_inputs(snapshot, package, contract)
            selected_narrow=not offchip if narrow is None else narrow
            profile = CoreBuildProfile(contract, tools, tools['calyx'].executable.parent.parent / 'share/calyx',
                                       narrow_immutable=selected_narrow, offchip_immutable=offchip,
                                       pool_private_capacity=stable, stable_scratch_pooling=stable,
                                       lut_memory_max_bits=lut_bits)
            output = root / 'build'
            output.mkdir()
            forbidden = output / 'expected_tokens.json'
            forbidden.write_text('[999999]')
            verification = output / 'verification'
            verification.mkdir()
            for filename in ('success.json', 'spec.json', 'expected.json'):
                (verification / filename).write_text('poisoned verification fixture')
            original_open, original_unlink = Path.open, Path.unlink
            def protected_open(path, *arguments, **kwargs):
                if path.resolve().is_relative_to(verification):
                    raise AssertionError('compiler read a verification fixture')
                return original_open(path, *arguments, **kwargs)
            def protected_unlink(path, *arguments, **kwargs):
                if path.resolve().is_relative_to(verification):
                    raise AssertionError('compiler modified a verification fixture')
                return original_unlink(path, *arguments, **kwargs)

            # Only the expensive model constructor is substituted. Capture,
            # import, linking, memory binding and native SV emission are real.
            with patch('compiler.generation.build_fixed_point_model', return_value=SmallModel()), \
                    patch.object(Path, 'open', protected_open), \
                    patch.object(Path, 'unlink', protected_unlink):
                receipt = build_core(inputs, profile, output)
            self.assertEqual([record['context'] for record in receipt.graphs], list(range(4, 20)))
            self.assertTrue(Path(receipt.output).is_file())
            interface_path=output/'initialized/external-interface/interface.json'
            self.assertTrue(interface_path.is_file(),'common core must export typed board-facing contract')
            interface=json.loads(interface_path.read_text())
            self.assertEqual(interface['entrypoint'],'main')
            self.assertEqual({m['runtime_port'] for m in interface['memories'] if 'runtime_port' in m},
                             {'prompt','generated_tokens','last_logits'})
            self.assertIn(str(interface_path),receipt.dependency_hashes)
            hardware = Path(receipt.output).read_text()
            if lut_bits and not selected_narrow:
                verilator=os.environ.get('LLM2FPGA_VERILATOR')
                self.assertTrue(verilator and Path(verilator).is_file(),'native Verilator required')
                checked=subprocess.run([verilator,'--lint-only','-Wno-fatal','--top-module','main',receipt.output],
                    capture_output=True,text=True,timeout=60)
                self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
            for role in ('prompt', 'generated_tokens', 'last_logits'):
                self.assertIn(role + '_read_data', hardware)
                self.assertIn(role + '_content_en', hardware)
            if offchip:
                bindings = json.loads((output / 'bindings.json').read_text())['bindings']
                self.assertTrue(any(b['implementation'] == 'ddr3' for b in bindings))
                self.assertTrue(all(not b['writable'] and b['read_latency'] == 0
                                    for b in bindings if b['implementation'] == 'ddr3'))
                self.assertNotIn('closure_ddr3_memory #', hardware)
                layout_path = output / 'ddr/layout.json'
                layout = json.loads(layout_path.read_text())
                image_path = output / 'ddr/images.bin'
                self.assertEqual(layout['words_per_frame'], 9)
                self.assertEqual(layout['memories'][0]['depth'], 3)
                self.assertEqual(int.from_bytes(image_path.read_bytes()[8:16], 'little'), 3)
                self.assertIn(str(layout_path), receipt.dependency_hashes)
                self.assertIn(str(image_path), receipt.dependency_hashes)

            self.assertTrue(all(not Path(name).is_relative_to(verification)
                                for name in receipt.dependency_hashes))
            for filename in ('success.json', 'spec.json', 'expected.json'):
                self.assertEqual((verification / filename).read_text(), 'poisoned verification fixture')
            self.assertNotIn(str(forbidden), receipt.dependency_hashes)
            verify_core_receipt(output, profile)
            if lut_bits:
                bindings=json.loads((output/'bindings.json').read_text())['bindings']
                self.assertTrue(all(b['implementation']=='lutram' and b['read_latency']==2 for b in bindings))
            if stable:
                for stage in ('normalized-index-scales', 'reduced-index-scales'):
                    self.assertTrue((output/'controller'/stage/'success.json').is_file())
                calyx_spec = json.loads((output/'controller/calyx-ir/spec.json').read_text())
                self.assertIn(str(output/'controller/reduced-index-scales/shared-functions.mlir'),
                              calyx_spec['input_hashes'])
                from scripts.pipeline.export_graph_core_synthesis import main as export_main
                exported = root/'synthesis'
                with contextlib.redirect_stdout(io.StringIO()):
                    export_main(['--core', str(output), '--output', str(exported),
                                 '--numerical-contract', str(contract),
                                 '--calyx-library', str(profile.calyx_library)])
                self.assertTrue((exported/'core-synthesis.json').is_file())
            forbidden.write_text('[-1, -2, -3]')
            from compiler.compile_core import main
            arguments = ['--snapshot', str(snapshot), '--package', str(package),
                         '--numerical-contract', str(contract), '--output', str(output),
                         '--calyx-library', str(profile.calyx_library), '--timeout-seconds', '120']
            for name, tool in tools.items():
                arguments += ['--' + name + '-tool', str(tool.executable)]
            if offchip:
                arguments += ['--offchip-immutable', '--no-narrow-immutable']
            elif not selected_narrow:
                arguments += ['--no-narrow-immutable']
            if stable:
                arguments += ['--pool-private-capacity', '--stable-scratch-pooling']
            if lut_bits:
                arguments += ['--lut-memory-max-bits',str(lut_bits)]
            stdout = io.StringIO()
            with patch('compiler.generation.build_fixed_point_model', return_value=SmallModel()), \
                    contextlib.redirect_stdout(stdout), \
                    patch.object(Path, 'open', protected_open), \
                    patch.object(Path, 'unlink', protected_unlink):
                self.assertEqual(main(arguments), 0)
            self.assertEqual(stdout.getvalue().strip(), receipt.identity_sha256,
                             'CLI and poisoned expected outputs must not change compilation')
            verify_core_receipt(output, profile)
            # A resume does not establish independence of a fresh compilation.
            # Rebuild into a new directory with different expected answers.
            fresh = root / 'fresh-build'
            fresh.mkdir()
            (fresh / 'expected_tokens.json').write_text('[123456, -99]')
            with patch('compiler.generation.build_fixed_point_model', return_value=SmallModel()):
                fresh_receipt = build_core(inputs, profile, fresh)
            verify_core_receipt(fresh, profile)
            self.assertEqual(Path(receipt.output).read_bytes(),
                             Path(fresh_receipt.output).read_bytes(),
                             'fresh compilation changed emitted SV with poisoned expected answers')
            for relative in ('ddr/images.bin',) if offchip else ():
                self.assertEqual((output / relative).read_bytes(),
                                 (fresh / relative).read_bytes())
            self.assertNotIn(str(fresh / 'expected_tokens.json'),
                             fresh_receipt.dependency_hashes)
            if offchip:
                image_path = output / 'ddr/images.bin'
                original_image = image_path.read_bytes()
                image_path.write_bytes(bytes(len(original_image)))
                with self.assertRaisesRegex(ValueError, 'core dependency changed'):
                    verify_core_receipt(output, profile)
                image_path.write_bytes(original_image)
                verify_core_receipt(output, profile)
            if offchip:
                import sys, litedram, litex, migen, shutil
                from compiler.ddr_transport import build_ddr_transport, verify_ddr_transport_receipt
                from compiler.litedram_export import export_controller_stage
                from test_graph_litedram_export import LiteDRAMExportTests
                pins=root/'board-pins.json'
                pins.write_text(json.dumps(LiteDRAMExportTests().pins()))
                native_sources={name:Path(module.__file__).parent for name,module in
                    [('litedram',litedram),('litex',litex),('migen',migen)]}
                controller=root/'native-controller'
                export_controller_stage(pins,controller,python=Path(sys.executable),sources=native_sources)
                transport=output/'transport'
                with patch.object(Path, 'open', protected_open), patch.object(Path, 'unlink', protected_unlink):
                    try:
                        joined=build_ddr_transport(output,profile,controller,transport,
                            yosys=Path(shutil.which('yosys')))
                    except Exception:
                        for log in (transport/'domains/elaboration').glob('*.log'):
                            print(log.name,log.read_text()[-2500:])
                        raise
                    verify_ddr_transport_receipt(transport,profile)
                self.assertIn(str(image_path),joined['dependency_hashes'])
                self.assertIn(str(controller/'controller.v'),joined['dependency_hashes'])
                self.assertTrue(Path(joined['output']).is_file())
                self.assertEqual(joined['core_identity'],receipt.identity_sha256)
                self.assertFalse(any(Path(name).is_relative_to(verification) for name in joined['dependency_hashes']))
                from scripts.pipeline.export_graph_ddr_transport import export_transport_stage
                standalone=root/'standalone-transport'
                exported=export_transport_stage(output,controller,standalone,
                    numerical_contract=profile.numerical_contract,
                    calyx_library=profile.calyx_library,python=Path(sys.executable),
                    yosys=Path(shutil.which('yosys')))
                standalone_dependencies=verify_stage_receipt(standalone)
                driver=Path(__file__).resolve().parents[1]/'scripts/pipeline/export_graph_ddr_transport.py'
                self.assertIn(str(driver),standalone_dependencies)
                self.assertIn(str(controller/'controller.v'),standalone_dependencies)
                self.assertIn(str(output/'core.json'),standalone_dependencies)
                self.assertTrue(exported.success)
                verify_ddr_transport_receipt(standalone/'transport',profile)
                original_controller=(controller/'controller.json').read_text()
                (controller/'controller.json').write_text('{}')
                with self.assertRaises(ValueError):verify_ddr_transport_receipt(transport,profile)
                with self.assertRaises(ValueError):build_ddr_transport(output,profile,controller,transport,yosys=Path(shutil.which('yosys')))
                self.assertFalse((transport/'transport.json').exists())
                (controller/'controller.json').write_text(original_controller)

            Path(receipt.output).write_text('changed hardware artifact\n')
            with self.assertRaisesRegex(ValueError, 'core dependency changed'):
                verify_core_receipt(output, profile)
