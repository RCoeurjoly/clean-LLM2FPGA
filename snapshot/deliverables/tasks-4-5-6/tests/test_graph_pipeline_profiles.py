"""The shared profile executes real native passes and preserves boundaries."""
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PipelineProfileTests(unittest.TestCase):
    def test_stable_pooling_selects_native_banks_and_index_sharing_order(self):
        from compiler.pipeline import integer_profile
        import inspect
        self.assertIn('stable_scratch_pooling', inspect.signature(integer_profile).parameters)
        stages = integer_profile(entrypoint='main', pool_private_capacity=True,
                                 stable_scratch_pooling=True)
        names = [s[0] for s in stages]
        start = names.index('flat-memory')
        self.assertEqual(names[start:start+5],
                         ['flat-memory', 'normalized-index-scales', 'shared-functions',
                          'reduced-index-scales', 'calyx-ir'])
        scratch = next(args for name, _, args, _ in stages if name == 'scratch')
        self.assertIn('--llm2fpga-pool-private-capacity=sharing-stable=true', scratch)
        self.assertNotIn('--llm2fpga-reuse-buffers', scratch)
        self.assertFalse(any(a.startswith('--llm2fpga-pool-sequential-scratch=') for a in scratch))
        reduced = next(args for name, _, args, _ in stages if name == 'reduced-index-scales')
        self.assertIn('--llm2fpga-strength-reduce-integer-constants=index-scales=true', reduced)
        default = integer_profile(entrypoint='main', pool_private_capacity=True)
        self.assertEqual(default, integer_profile(entrypoint='main', pool_private_capacity=True,
                                                 stable_scratch_pooling=False))
        self.assertNotIn('normalized-index-scales', [s[0] for s in default])

    def test_stable_pooling_rejects_invalid_policy_before_execution(self):
        from compiler.pipeline import integer_profile
        for policy in ({'entrypoint': 'main', 'pool_private_capacity': True,
                        'stable_scratch_pooling': 1},
                       {'entrypoint': 'main', 'stable_scratch_pooling': True},
                       {'pool_private_capacity': True, 'stable_scratch_pooling': True}):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, 'stable scratch'):
                integer_profile(**policy)

    def test_native_cleanup_is_declared_in_shared_export_profiles(self):
        from compiler.pipeline import integer_profile, CALYX_RESOURCE_PASSES
        for policy in ({}, {'entrypoint': 'main'},
                       {'entrypoint': 'main', 'bytecode': True, 'runtime_roles': True}):
            with self.subTest(policy=policy):
                arguments = next(args for name, _, args, _ in integer_profile(**policy)
                                 if name == 'sv')
                self.assertIn('cell-share:bounds=1,-1,-1', arguments)
                self.assertIn('cell-share:retire-dead-cells=true', arguments)
                self.assertEqual(arguments.count('cell-share:retire-dead-cells=true'), 1)
                self.assertEqual(arguments.count('simplify-with-control:retire-dead-groups=true'), 1)
        self.assertIn('cell-share:retire-dead-cells=true', CALYX_RESOURCE_PASSES)
        self.assertIn('simplify-with-control:retire-dead-groups=true', CALYX_RESOURCE_PASSES)

    def test_generation_runtime_roles_are_an_explicit_native_pass(self):
        from compiler.pipeline import integer_profile
        self.assertFalse(any('--llm2fpga-resolve-runtime-memories' in args
                             for _, _, args, _ in integer_profile()))
        stages = integer_profile(entrypoint='main', bytecode=True, runtime_roles=True)
        args = next(args for name, _, args, _ in stages if name == 'memory-contracts')
        self.assertIn('--llm2fpga-resolve-runtime-memories', args)
        self.assertLess(args.index('--llm2fpga-complete-calyx-memory-contracts'),
                        args.index('--llm2fpga-resolve-runtime-memories'))
        with self.assertRaisesRegex(ValueError, 'runtime roles require'):
            integer_profile(runtime_roles=True)
        with self.assertRaisesRegex(ValueError, 'runtime role policy'):
            integer_profile(entrypoint='main', runtime_roles=1)
    def test_invalid_entrypoint_is_rejected_before_compilation(self):
        from compiler.pipeline import compile_integer_graph
        for entrypoint in ('', 'main other', 'main,other', 'main{other}', 3, True):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'entrypoint'):
                    compile_integer_graph(Path('/missing'), Path(directory), {},
                                          target='buffers', entrypoint=entrypoint)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_integer_argmax_ties_and_extrema_execute_through_shared_passes(self):
        import subprocess
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import INTEGER_PROFILE, TORCH_INTEGER_PASSES
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        runner = os.environ.get('LLM2FPGA_MLIR_RUNNER')
        self.assertTrue(tool and runner, 'pinned native driver and runner required')
        class Kernel(torch.nn.Module):
            def __init__(self, values):
                super().__init__()
                self.register_parameter('values', torch.nn.Parameter(
                    torch.tensor(values, dtype=torch.int64), requires_grad=False))
            def forward(self):
                return torch.argmax(self.values)
        pipeline = TORCH_INTEGER_PASSES[:-1] + ',llm2fpga-test-scalar-entry)'
        passes = [arg for name, _, args, _ in INTEGER_PROFILE
                  if name in {'buffers', 'scratch', 'loops', 'structured'} for arg in args]
        for values in ([9, 9, -1], [-9, -3, -3], [0, 0, 0],
                       [-(1 << 63), -(1 << 63)], [0, (1 << 63)-1, (1 << 63)-1]):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = Kernel(values)
                raw, standard, llvm = [root / name for name in ('raw.mlir', 'standard.mlir', 'llvm.mlir')]
                raw.write_text(serialize_raw_torch(import_raw_torch(torch.export.export(model, ()))))
                commands = [
                    [tool, str(raw), '--pass-pipeline=' + pipeline, '-o', str(standard)],
                    [tool, str(standard), *passes, '--convert-scf-to-cf', '--convert-cf-to-llvm',
                     '--convert-arith-to-llvm', '--convert-index-to-llvm', '--convert-func-to-llvm',
                     '--finalize-memref-to-llvm', '--reconcile-unrealized-casts', '-o', str(llvm)],
                    [runner, str(llvm), '-e', 'main', '--entry-point-result=i64']]
                for command in commands:
                    result = subprocess.run(command, text=True, capture_output=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)
                # Independent first-maximum reference is evaluated after compile.
                self.assertEqual(int(result.stdout.strip()), values.index(max(values)))
                self.assertEqual(int(model()), values.index(max(values)))

    def test_pointwise_chain_is_fused_before_bufferization(self):
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import CompilerTool, compile_integer_graph
        class Kernel(torch.nn.Module):
            def forward(self, values):
                return (values * 3 + 7).clamp(-128, 127)
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        exported = torch.export.export(Kernel(), (torch.tensor([-100, 0, 100], dtype=torch.int64),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.mlir'
            raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
            result = compile_integer_graph(raw, root / 'build',
                                           {'torch': CompilerTool(Path(tool))}, target='buffers')
            before = (root / 'build/standard/standard.mlir').read_text()
            after = result.output.read_text()
            self.assertGreater(before.count('linalg.generic'), 1)
            self.assertEqual(after.count('linalg.generic'), 1)

    def test_native_reduction_reaches_structured_ir_with_stage_receipts(self):
        from compiler.pipeline import CompilerTool, compile_integer_graph
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        source = ROOT / 'compiler/tests/torch/serial-gemv.mlir'
        original = source.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            result = compile_integer_graph(source, Path(directory),
                                           {'torch': CompilerTool(Path(tool))}, target='structured')
            self.assertEqual(source.read_bytes(), original)
            self.assertIn('scf.while', result.output.read_text())
            self.assertNotIn('torch.', result.output.read_text())
            self.assertEqual([r.name for r in result.receipts],
                             ['standard', 'buffers', 'scratch', 'loops', 'structured'])
            self.assertTrue(all(r.success for r in result.receipts))
            self.assertTrue((Path(directory) / 'structured/success.json').is_file())

    def test_unknown_target_is_rejected_before_any_stage_runs(self):
        from compiler.pipeline import compile_integer_graph
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'unknown target'):
                compile_integer_graph(ROOT / 'compiler/tests/torch/serial-gemv.mlir',
                                      Path(directory), {}, target='unchecked-sv')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_recompile_invalidates_downstream_acceptance(self):
        from compiler.pipeline import CompilerTool, compile_integer_graph
        from compiler.stages import StageError
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'raw.mlir'
            source.write_bytes((ROOT / 'compiler/tests/torch/serial-gemv.mlir').read_bytes())
            tools = {'torch': CompilerTool(Path(tool))}
            compile_integer_graph(source, root / 'build', tools, target='structured')
            source.write_text('invalid MLIR')
            with self.assertRaises(StageError):
                compile_integer_graph(source, root / 'build', tools, target='structured')
            self.assertFalse((root / 'build/structured/success.json').exists())

    def test_policy_change_invalidates_optional_stage_acceptance(self):
        import sys
        from compiler.pipeline import CompilerTool, compile_integer_graph
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            optional = ('normalized-index-scales', 'shared-functions', 'reduced-index-scales')
            for name in optional:
                (root / name).mkdir()
                (root / name / 'success.json').write_text('{}')
            with self.assertRaises(FileNotFoundError):
                compile_integer_graph(root / 'missing.mlir', root,
                    {'torch': CompilerTool(Path(sys.executable))}, target='buffers')
            self.assertTrue(all(not (root / name / 'success.json').exists() for name in optional))

    def test_pytorch_source_locations_survive_native_stage_boundaries(self):
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import CompilerTool, compile_integer_graph
        class Kernel(torch.nn.Module):
            def forward(self, values):
                return values + 3
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        exported = torch.export.export(Kernel(), (torch.tensor([1, 2], dtype=torch.int64),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.mlir'
            raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
            result = compile_integer_graph(raw, root / 'build',
                                           {'torch': CompilerTool(Path(tool))}, target='structured')
            for receipt in result.receipts:
                output = next((root / 'build' / receipt.name).glob('*.mlir'))
                self.assertIn(str(Path(__file__).resolve()), output.read_text(), receipt.name)

    def test_sliced_floor_division_reaches_supported_structured_operations(self):
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import CompilerTool, compile_integer_graph
        class Kernel(torch.nn.Module):
            def forward(self, values):
                return torch.div(values[:, 1:3], -3, rounding_mode='floor').sum()
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        exported = torch.export.export(Kernel(), (torch.arange(8, dtype=torch.int64).reshape(2, 4),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.mlir'
            raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
            result = compile_integer_graph(raw, root / 'build',
                                           {'torch': CompilerTool(Path(tool))}, target='structured')
            structured = result.output.read_text()
            for unsupported in ('memref.copy', 'memref.subview', 'affine.apply', 'arith.floordivsi'):
                self.assertNotIn(unsupported, structured)
            self.assertIn('scf.while', structured)

    def test_calyx_boundary_removes_unused_memref_globals(self):
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import CompilerTool, compile_integer_graph
        class Kernel(torch.nn.Module):
            def forward(self, values):
                return torch.div(values[:, 1:3], -3, rounding_mode='floor').sum()
        tools = {}
        for name, variable in (('torch', 'LLM2FPGA_TORCH_PASS_TOOL'),
                               ('circt', 'LLM2FPGA_CIRCT_TOOL'),
                               ('memory', 'LLM2FPGA_CALYX_PASS_TOOL')):
            path = os.environ.get(variable)
            self.assertTrue(path and Path(path).is_file(), variable + ' is required')
            tools[name] = CompilerTool(Path(path))
        exported = torch.export.export(Kernel(), (torch.arange(8, dtype=torch.int64).reshape(2, 4),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.mlir'
            raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
            result = compile_integer_graph(raw, root / 'build', tools, target='calyx-ir')
            self.assertNotIn('memref.global', result.output.read_text())
            self.assertIn('calyx.component', result.output.read_text())

    def test_integer_absolute_value_is_legalized_before_calyx(self):
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import CompilerTool, compile_integer_graph
        class Kernel(torch.nn.Module):
            def forward(self, values):
                return values.abs()
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch driver required')
        exported = torch.export.export(Kernel(), (torch.tensor([-7, 0, 7], dtype=torch.int64),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw.mlir'
            raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
            result = compile_integer_graph(raw, root / 'build',
                                           {'torch': CompilerTool(Path(tool))}, target='structured')
            self.assertNotIn('math.absi', result.output.read_text())
