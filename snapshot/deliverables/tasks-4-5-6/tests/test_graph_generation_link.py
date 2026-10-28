"""Native generation control must consume the preceding computed token."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


def fixture(lengths=range(4, 20), bad_shape=False):
    functions = []
    for n in lengths:
        shape = n + 1 if bad_shape and n == 8 else n
        functions.append(f'''func.func private @step_{n}(
 %weights: memref<1xi64> {{llm2fpga.input_origin = {{kind = "BUFFER", name = "weight", target = "weight", sha256 = "{'1'*64}"}}}},
 %tokens: memref<1x{shape}xi64> {{llm2fpga.input_origin = {{kind = "USER_INPUT", name = "tokens"}}}},
 %token: memref<i64> {{bufferize.result}}, %logits: memref<3xi64> {{bufferize.result}})
 attributes {{llm2fpga.context_length = {n} : i64}} {{
 %zero = arith.constant 0 : index
 %last = arith.constant {n-1} : index
 %one = arith.constant 1 : index
 %two = arith.constant 2 : index
 %prior = memref.load %tokens[%zero, %last] : memref<1x{shape}xi64>
 %weight = memref.load %weights[%zero] : memref<1xi64>
 %next = arith.addi %prior, %weight : i64
 memref.store %next, %token[] : memref<i64>
 memref.store %next, %logits[%zero] : memref<3xi64>
 %second = arith.addi %next, %weight : i64
 memref.store %second, %logits[%one] : memref<3xi64>
 %third = arith.addi %second, %weight : i64
 memref.store %third, %logits[%two] : memref<3xi64>
 return
}}''')
    return 'module {\n' + '\n'.join(functions) + '\n}'


class GenerationLinkTests(unittest.TestCase):
    def link(self, source):
        return subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--llm2fpga-link-generation'], input=source,
            text=True, capture_output=True, timeout=60)

    def test_missing_context_is_rejected(self):
        result = self.link(fixture(range(4, 19)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordered contexts 4 through 19', result.stderr)

    def test_context_shape_mismatch_is_rejected(self):
        result = self.link(fixture(bad_shape=True))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime context geometry', result.stderr)

    def test_all_steps_are_linked_to_runtime_context_storage(self):
        result = self.link(fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('func.func @main', result.stdout)
        self.assertEqual(result.stdout.count('call @step_'), 16)
        self.assertIn('memref<1x20xi64>', result.stdout)
        self.assertIn('memref<16xi64>', result.stdout)
        for role in ('prompt', 'generated_tokens', 'last_logits'):
            self.assertEqual(result.stdout.count(
                f'llm2fpga.runtime_port = "{role}"'), 1)

    def test_changed_frozen_identity_is_rejected(self):
        source = fixture().replace('sha256 = "' + '1'*64 + '"',
                                   'sha256 = "' + '2'*64 + '"', 1)
        result = self.link(source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frozen input contract', result.stderr)

    def test_runtime_feedback_and_last_logits_execute(self):
        import ctypes
        import numpy as np
        from torch_mlir import ir
        from torch_mlir.execution_engine import ExecutionEngine
        from torch_mlir.runtime import get_ranked_memref_descriptor
        linked = self.link(fixture())
        self.assertEqual(linked.returncode, 0, linked.stderr)
        lowered = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--convert-scf-to-cf', '--convert-cf-to-llvm',
            '--convert-arith-to-llvm', '--convert-index-to-llvm',
            '--convert-func-to-llvm', '--finalize-memref-to-llvm',
            '--reconcile-unrealized-casts'], input=linked.stdout,
            text=True, capture_output=True, timeout=60)
        self.assertEqual(lowered.returncode, 0, lowered.stderr)
        with ir.Context():
            module = ir.Module.parse(lowered.stdout)
            engine = ExecutionEngine(module)
            for weight, prompt in ((1, [2, 3, 4, 5]), (-3, [123, -4, 9, 20])):
                weights = np.array([weight], dtype=np.int64)
                tokens = np.array([prompt], dtype=np.int64)
                generated = np.full(16, -999, dtype=np.int64)
                logits = np.full(3, -999, dtype=np.int64)
                arrays = (weights, tokens, generated, logits)
                descriptors = [get_ranked_memref_descriptor(a) for a in arrays]
                pointers = [ctypes.pointer(ctypes.pointer(d)) for d in descriptors]
                engine.invoke('main', *pointers)
                expected = np.array([prompt[-1] + weight * (i + 1)
                                     for i in range(16)], dtype=np.int64)
                np.testing.assert_array_equal(generated, expected)
                np.testing.assert_array_equal(logits,
                    [expected[-1] + weight * i for i in range(3)])
                np.testing.assert_array_equal(tokens, [prompt])
                np.testing.assert_array_equal(weights, [weight])

    def test_linked_controller_lowers_through_native_calyx(self):
        from compiler.pipeline import integer_profile
        linked = self.link(fixture())
        self.assertEqual(linked.returncode, 0, linked.stderr)
        tools = {'torch': os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                 'circt': os.environ['LLM2FPGA_CIRCT_TOOL'],
                 'memory': os.environ['LLM2FPGA_MEMORY_TOOL']}
        source = linked.stdout
        for name, tool, arguments, _ in integer_profile(entrypoint='main'):
            if name in {'standard', 'buffers', 'futil', 'sv'}: continue
            result = subprocess.run([tools[tool], *arguments], input=source,
                text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, name + ': ' + result.stderr[:2000])
            source = result.stdout
        self.assertIn('calyx.component @main', source)
        self.assertIn('calyx.invoke', source)
        for role in ('prompt', 'generated_tokens', 'last_logits'):
            self.assertEqual(source.count(
                f'llm2fpga.runtime_port = "{role}"'), 1)
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / 'inventory.json'
            result = subprocess.run([tools['memory'], '--verify-each',
                '--llm2fpga-resolve-runtime-memories',
                '--llm2fpga-export-memory-inventory=inventory=' + str(inventory)],
                input=source, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            physical = {memory['runtime_port']: memory
                for memory in json.loads(inventory.read_text())['memories']
                if memory['physical'] and 'runtime_port' in memory}
            self.assertEqual(set(physical), {'prompt', 'generated_tokens', 'last_logits'})
            self.assertEqual({role: memory['depth'] for role, memory in physical.items()},
                {'prompt': 4, 'generated_tokens': 16, 'last_logits': 3})

    def test_separate_context_modules_import_with_typed_symbol_renaming(self):
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for n in range(4, 20):
                path = Path(directory) / f'context-{n}.mlir'
                source = fixture(range(n, n+1)).replace(f'@step_{n}', '@main')
                source = source.replace('%next = arith.addi %prior, %weight : i64',
                    '%next = func.call @helper(%prior, %weight) : (i64, i64) -> i64')
                helper = '''func.func private @helper(%a: i64, %b: i64) -> i64 {
 %result = arith.addi %a, %b : i64
 return %result : i64
}'''
                path.write_text(source[:-1] + helper + '\n}')
                files.append(str(path))
            result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                '--verify-each', '--llm2fpga-import-generation=files=' + ','.join(files),
                '--llm2fpga-link-generation'], input='module {}',
                text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count('call @generation_ctx_'), 32)
            self.assertIn('@generation_ctx_4_helper', result.stdout)
            self.assertIn('@generation_ctx_19_helper', result.stdout)
            self.assertIn('func.func @main', result.stdout)

    def test_context_import_rejects_missing_file(self):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--llm2fpga-import-generation=files=' + ','.join(['/missing/context.mlir'] * 16)],
            input='module {}', text=True, capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot parse context module', result.stderr)
