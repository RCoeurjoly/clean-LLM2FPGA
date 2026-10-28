"""Qualify the native out-parameter ABI used by generation linking.

These checks cover metadata and ordering, not generation numerical behavior.
"""
import os
import subprocess
import unittest


class GenerationAbiTests(unittest.TestCase):
    def test_shared_buffer_profile_marks_generation_output(self):
        from compiler.pipeline import INTEGER_PROFILE
        arguments = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'buffers')
        output_pass = next(arg for arg in arguments if arg.startswith('--buffer-results-to-out-params='))
        source = '''module {
 func.func @main(%input: memref<4xi64>) -> memref<4xi64> {
  %output = memref.alloc() : memref<4xi64>
  memref.copy %input, %output : memref<4xi64> to memref<4xi64>
  return %output : memref<4xi64>
 }
}'''
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', output_pass], input=source, text=True,
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('memref<4xi64> {bufferize.result}', result.stdout)

    def test_output_buffers_are_marked_and_input_origin_is_preserved(self):
        source = '''module {
 func.func @main(%tokens: memref<1x4xi64> {llm2fpga.input_origin = {kind = "USER_INPUT", name = "tokens"}}) -> (memref<i64>, memref<50257xi64>) {
  %token = memref.alloc() : memref<i64>
  %logits = memref.alloc() : memref<50257xi64>
  return %token, %logits : memref<i64>, memref<50257xi64>
 }
}'''
        result = subprocess.run([
            os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
            '--buffer-results-to-out-params=add-result-attr=true '
            'hoist-static-allocs=true modify-public-functions=true'],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('bufferize.result'), 2)
        self.assertIn('memref<i64> {bufferize.result}', result.stdout)
        self.assertIn('memref<50257xi64> {bufferize.result}', result.stdout)
        self.assertLess(result.stdout.index('memref<i64> {bufferize.result}'),
                        result.stdout.index('memref<50257xi64> {bufferize.result}'))
        self.assertEqual(result.stdout.count('llm2fpga.input_origin'), 1)
        self.assertIn('kind = "USER_INPUT", name = "tokens"', result.stdout)
