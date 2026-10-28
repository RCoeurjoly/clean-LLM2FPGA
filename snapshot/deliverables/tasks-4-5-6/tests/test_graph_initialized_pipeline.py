"""Initialized memory stages must authenticate every source-level dependency."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from compiler.pipeline import CompilerTool
from compiler.stages import StageReceipt, digest


class InitializedPipelineTests(unittest.TestCase):
    def test_native_bound_memory_uses_bytecode_and_exports_sv(self):
        from compiler.pipeline import bind_initialized_memories
        from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources
        from test_graph_memory_binding import SOURCE
        tools = {name: CompilerTool(Path(os.environ[variable])) for name, variable in
            (('memory', 'LLM2FPGA_CALYX_PASS_TOOL'),
             ('translate', 'LLM2FPGA_CALYX_TRANSLATE_TOOL'), ('calyx', 'LLM2FPGA_CALYX_TOOL'))}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir = root / 'source.mlir'
            ir.write_text(SOURCE.replace('%false = hw.constant false',
                '%bridge.in, %bridge.out = calyx.std_wire @bridge : i1, i1\n    %false = hw.constant false'))
            image = root / 'weights.hex'
            image.write_text('01\nff\n07\n')
            manifest = root / 'bindings.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'bindings': [dict(
                component='main', cell='m', width=8, depth=3, address_width=2,
                implementation='bram', read_latency=2, writable=False,
                init_file=image.name, init_sha256=digest(image))]}))
            primitive = root / 'closure_memory.sv'
            primitive.write_text(emit_memory_sources([
                MemorySpec('fixture', 8, 3, 'bram', 2, False, image.name)])['sv'])
            result = bind_initialized_memories(ir, manifest, primitive, root / 'compiled', tools,
                calyx_library=tools['calyx'].executable.parents[1] / 'share/calyx')
            self.assertTrue(result.output.is_file())
            futil = (root / 'compiled/futil/kernel.futil').read_text()
            self.assertIn('read_retire', futil,
                          'common binding pipeline must retire prior memory requests')
            bytecode = root / 'compiled/memory-binding/calyx.mlirbc'
            self.assertTrue(bytecode.is_file(), 'native binding must serialize bytecode')
            with bytecode.open('rb') as stream:
                self.assertEqual(stream.read(4), b'ML\xefR')
            with (root / 'compiled/memory-validation/calyx.mlirbc').open('rb') as stream:
                self.assertEqual(stream.read(4), b'ML\xefR')
            self.assertTrue(all(receipt.success for receipt in result.receipts))

    def test_binding_stages_retain_image_manifest_and_primitive_identities(self):
        from compiler.pipeline import bind_initialized_memories
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir = root / 'calyx.mlir'
            ir.write_text('module {}')
            image = root / 'weights.hex'
            image.write_text('01\n')
            manifest = root / 'bindings.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'bindings': [
                {'init_file': image.name, 'init_sha256': digest(image)}]}))
            primitive = root / 'closure_memory.sv'
            primitive.write_text('// owned primitive source')
            library = root / 'library'
            library.mkdir()
            (library / 'core.futil').write_text('// pinned library')
            executable = Path(sys.executable).resolve()
            tools = {name: CompilerTool(executable) for name in ('memory', 'translate', 'calyx')}
            specs = []
            def stage(spec, directory):
                specs.append(spec)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / spec.outputs[0]).write_text('test boundary')
                return StageReceipt(1, spec.name, True, '', '', {}, 0, None, '')
            with patch('compiler.pipeline.run_stage', side_effect=stage):
                result = bind_initialized_memories(ir, manifest, primitive,
                    root / 'compile', tools, calyx_library=library, synthesis=True)
            self.assertIn('--synthesis', specs[-1].argv)
            self.assertIn('--nested', specs[-1].argv)
            self.assertIn('cell-share:bounds=1,-1,-1', specs[-1].argv)
            self.assertEqual(len(result.receipts), 4)
            self.assertEqual([s.name for s in specs],
                ['memory-validation', 'memory-binding', 'futil', 'sv'])
            for spec in specs:
                for path in (manifest, image, primitive):
                    self.assertEqual(spec.input_hashes[str(path)], digest(path))
            self.assertIn('manifest-sha256=' + digest(manifest), ' '.join(specs[0].argv))
            self.assertIn('primitive-source-sha256=' + digest(primitive), ' '.join(specs[1].argv))

    def test_graph_annotation_is_a_hashed_boundary_before_lowering(self):
        from compiler.pipeline import compile_integer_graph
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / 'raw.mlir'
            raw.write_text('module {}')
            manifest = root / 'graph-inputs.json'
            manifest.write_text('{}')
            specs = []
            def stage(spec, directory):
                specs.append(spec)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / spec.outputs[0]).write_text('module {}')
                return StageReceipt(1, spec.name, True, '', '', {}, 0, None, '')
            with patch('compiler.pipeline.run_stage', side_effect=stage):
                compile_integer_graph(raw, root / 'compile',
                    {'torch': CompilerTool(Path(sys.executable).resolve())},
                    target='standard', graph_inputs=manifest)
            self.assertEqual([spec.name for spec in specs], ['graph-inputs', 'standard'])
            self.assertEqual(specs[0].input_hashes[str(manifest)], digest(manifest))
            self.assertIn('manifest-sha256=' + digest(manifest), ' '.join(specs[0].argv))

    def test_inventory_declares_native_json_and_ir_outputs(self):
        from compiler.pipeline import export_memory_inventory
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir = root / 'input.mlir'
            ir.write_text('module {}')
            with patch('compiler.pipeline.run_stage') as native:
                inventory, receipt = export_memory_inventory(ir, root / 'inventory',
                    CompilerTool(Path(sys.executable).resolve()))
            spec = native.call_args.args[0]
            self.assertEqual(spec.outputs, ('inventory.json', 'calyx.mlirbc'))
            self.assertIn('--emit-bytecode', spec.argv)
            self.assertIn('--llm2fpga-export-memory-inventory=inventory=inventory.json', spec.argv)
            self.assertEqual(inventory, root / 'inventory' / 'inventory.json')

    def test_invalid_image_paths_are_rejected_before_native_execution(self):
        from compiler.pipeline import bind_initialized_memories
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ir, manifest, primitive = [root / name for name in ('ir.mlir', 'bindings.json', 'primitive.sv')]
            ir.write_text('module {}')
            primitive.write_text('// source')
            manifest.write_text(json.dumps({'schema_version': 1, 'bindings': [
                {'init_file': '../escape.hex', 'init_sha256': '0' * 64}]}))
            with patch('compiler.pipeline.run_stage') as native:
                with self.assertRaisesRegex(ValueError, 'local image'):
                    bind_initialized_memories(ir, manifest, primitive, root / 'out', {},
                                              calyx_library=root)
                native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
