"""Generation orchestration rejects incomplete or stale compiler boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class GenerationDriverTests(unittest.TestCase):
    def test_policy_change_invalidates_optional_stage_acceptance(self):
        from compiler.generation_driver import lower_generation_controller
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            optional = ('normalized-index-scales', 'shared-functions', 'reduced-index-scales')
            for name in optional:
                (root / name).mkdir()
                (root / name / 'success.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'verified compiler boundary'):
                lower_generation_controller(root / 'missing', root, {})
            self.assertTrue(all(not (root / name / 'success.json').exists() for name in optional))

    def test_missing_contexts_are_rejected_before_tools_or_output_creation(self):
        from compiler.generation_driver import link_generation_functions
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with patch('compiler.generation_driver.run_stage') as run:
                with self.assertRaisesRegex(ValueError, 'contexts 4 through 19'):
                    link_generation_functions((), output, object())
                run.assert_not_called()
            self.assertFalse(output.exists())

    def test_reordered_contexts_are_rejected_before_tools(self):
        from compiler.generation_driver import ContextFunction, link_generation_functions
        contexts = tuple(ContextFunction(n, Path('/missing')) for n in reversed(range(4, 20)))
        with tempfile.TemporaryDirectory() as directory:
            with patch('compiler.generation_driver.run_stage') as run:
                with self.assertRaisesRegex(ValueError, 'contexts 4 through 19'):
                    link_generation_functions(contexts, Path(directory), object())
                run.assert_not_called()

    def test_missing_verified_boundary_is_rejected_before_tools(self):
        from compiler.generation_driver import ContextFunction, link_generation_functions
        with tempfile.TemporaryDirectory() as directory:
            contexts = tuple(ContextFunction(n, Path(directory) / str(n)) for n in range(4, 20))
            with patch('compiler.generation_driver.run_stage') as run:
                with self.assertRaisesRegex(ValueError, 'verified buffer boundary'):
                    link_generation_functions(contexts, Path(directory) / 'out', object())
                run.assert_not_called()

    def boundaries(self, root):
        from dataclasses import asdict
        import sys
        from compiler.generation_driver import ContextFunction
        from compiler.stages import StageSpec, _identity, digest
        tool = Path(sys.executable).resolve()
        contexts = []
        for n in range(4, 20):
            directory = root / f'context-{n}'
            directory.mkdir()
            source, buffer = directory / 'source.mlir', directory / 'buffers.mlir'
            source.write_text('module {}\n')
            buffer.write_text('module {}\n')
            spec = asdict(StageSpec('buffers', (str(tool),),
                {str(source): digest(source)}, {str(tool): digest(tool)}, {}, ('buffers.mlir',)))
            spec_hash = _identity(spec)
            outputs = {'buffers.mlir': digest(buffer)}
            receipt = dict(schema_version=1, name='buffers', success=True,
                spec_sha256=spec_hash,
                identity_sha256=_identity({'spec_sha256': spec_hash, 'output_hashes': outputs}),
                output_hashes=outputs, exit_code=0, signal=None, diagnostic='')
            (directory / 'spec.json').write_text(json.dumps(spec))
            (directory / 'success.json').write_text(json.dumps(receipt))
            contexts.append(ContextFunction(n, directory))
        return tuple(contexts)

    def test_stale_input_rejects_and_invalidates_old_success(self):
        from compiler.generation_driver import link_generation_functions
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts = self.boundaries(root)
            (contexts[8].buffer_stage / 'source.mlir').write_text('changed input')
            output = root / 'output'
            (output / 'linked').mkdir(parents=True)
            old = output / 'linked/success.json'
            old.write_text('{}')
            with patch('compiler.generation_driver.run_stage') as run:
                with self.assertRaisesRegex(ValueError, 'stale stage dependency'):
                    link_generation_functions(contexts, output, object())
                run.assert_not_called()
            self.assertFalse(old.exists())

    def test_stale_buffer_ir_is_rejected_before_tools(self):
        from compiler.generation_driver import link_generation_functions
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts = self.boundaries(root)
            (contexts[6].buffer_stage / 'buffers.mlir').write_text('changed output')
            with patch('compiler.generation_driver.run_stage') as run:
                with self.assertRaisesRegex(ValueError, 'stale buffer IR'):
                    link_generation_functions(contexts, root / 'out', object())
                run.assert_not_called()

    def test_non_mapping_dependency_identities_are_rejected_before_tools(self):
        from compiler.generation_driver import link_generation_functions
        from compiler.stages import _identity
        for kind in ('input_hashes', 'tool_hashes'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                contexts = self.boundaries(root)
                stage = contexts[0].buffer_stage
                spec = json.loads((stage / 'spec.json').read_text())
                spec[kind] = ['invalid dependency mapping']
                receipt = json.loads((stage / 'success.json').read_text())
                receipt['spec_sha256'] = _identity(spec)
                receipt['identity_sha256'] = _identity({
                    'spec_sha256': receipt['spec_sha256'],
                    'output_hashes': receipt['output_hashes']})
                (stage / 'spec.json').write_text(json.dumps(spec))
                (stage / 'success.json').write_text(json.dumps(receipt))
                with patch('compiler.generation_driver.run_stage') as run:
                    with self.assertRaisesRegex(ValueError, 'dependency identities must be a mapping'):
                        link_generation_functions(contexts, root / 'out', object())
                    run.assert_not_called()

    def test_verified_native_boundaries_link_through_owned_driver(self):
        import os
        from test_graph_generation_link import fixture
        from compiler.generation_driver import ContextFunction, link_generation_functions, lower_generation_controller
        from compiler.pipeline import CompilerTool
        from compiler.stages import StageSpec, digest, run_stage
        tool = CompilerTool(Path(os.environ['LLM2FPGA_TORCH_PASS_TOOL']))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contexts = []
            for n in range(4, 20):
                source = root / f'context-{n}.mlir'
                source.write_text(fixture(range(n, n+1)).replace(f'@step_{n}', '@main'))
                stage = root / f'context-{n}'
                run_stage(StageSpec('buffers',
                    (str(tool.executable), str(source), '--verify-each', '-o', 'buffers.mlir'),
                    {str(source): digest(source)}, tool.identities(),
                    {'scope': 'authored bufferized controller fixture'}, ('buffers.mlir',)), stage)
                contexts.append(ContextFunction(n, stage))
            linked = link_generation_functions(tuple(contexts), root / 'out', tool)
            self.assertEqual(len(linked.receipts), 1)
            self.assertTrue(linked.receipts[0].success)
            self.assertEqual(linked.output.read_text().count('call @generation_ctx_'), 16)
            spec = json.loads((linked.output.parent / 'spec.json').read_text())
            self.assertEqual(len(spec['input_hashes']), 49)
            lowered = lower_generation_controller(linked.output.parent, root / 'lowered',
                {'torch': tool,
                 'circt': CompilerTool(Path(os.environ['LLM2FPGA_CIRCT_TOOL'])),
                 'memory': CompilerTool(Path(os.environ['LLM2FPGA_MEMORY_TOOL']))})
            self.assertTrue(all(receipt.success for receipt in lowered.receipts))
            self.assertEqual([receipt.name for receipt in lowered.receipts],
                ['scratch', 'loops', 'structured', 'flat-memory', 'shared-functions', 'calyx-ir', 'memory-contracts'])
            with lowered.output.open('rb') as stream:
                self.assertEqual(stream.read(4), b'ML\xefR',
                                 'production controller must retain native bytecode')
            import subprocess
            decoded = root / 'controller-inspection.mlir'
            subprocess.run([os.environ['LLM2FPGA_MEMORY_TOOL'], str(lowered.output),
                            '--verify-each', '-o', str(decoded)], check=True,
                           capture_output=True, timeout=120)
            self.assertIn('calyx.component @main', decoded.read_text())
            inventory = root / 'runtime-inventory.json'
            subprocess.run([os.environ['LLM2FPGA_MEMORY_TOOL'], str(lowered.output),
                '--llm2fpga-export-memory-inventory=inventory=' + str(inventory)],
                check=True, capture_output=True, timeout=120)
            self.assertEqual({m['runtime_port'] for m in
                json.loads(inventory.read_text())['memories']
                if m['physical'] and 'runtime_port' in m},
                {'prompt', 'generated_tokens', 'last_logits'})
            # A changed linked computation cannot inherit descendant acceptance.
            linked.output.write_text('module {}\n')
            with self.assertRaisesRegex(ValueError, 'stale linked IR'):
                lower_generation_controller(linked.output.parent, root / 'lowered', {})
            self.assertTrue(all(not (root / 'lowered' / name / 'success.json').exists()
                for name in ('scratch', 'loops', 'structured', 'flat-memory',
                             'calyx-ir', 'memory-contracts')))
