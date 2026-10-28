"""Native producer tiling must retain the complete signed MAC reduction."""
import os
from pathlib import Path
import subprocess
import unittest


class ProducerSliceTests(unittest.TestCase):
    def fixture(self):
        return '''module {
func.func @project(%x: tensor<4x3xi64>, %w: tensor<6x3xi64>) -> tensor<1x6xi64> {
 %zero = arith.constant 0 : i64
 %empty = tensor.empty() : tensor<4x6xi64>
 %init = linalg.fill ins(%zero : i64) outs(%empty : tensor<4x6xi64>) -> tensor<4x6xi64>
 %full = linalg.generic {
  indexing_maps = [affine_map<(r,o,k)->(r,k)>, affine_map<(r,o,k)->(o,k)>, affine_map<(r,o,k)->(r,o)>],
  iterator_types = ["parallel", "parallel", "reduction"]}
  ins(%x, %w : tensor<4x3xi64>, tensor<6x3xi64>) outs(%init : tensor<4x6xi64>) {
 ^bb0(%a: i64, %b: i64, %c: i64):
  %product = arith.muli %a, %b : i64
  %sum = arith.addi %c, %product : i64
  linalg.yield %sum : i64
 } -> tensor<4x6xi64>
 %row = tensor.extract_slice %full[3,0] [1,6] [1,1] : tensor<4x6xi64> to tensor<1x6xi64>
 return %row : tensor<1x6xi64>
} }'''

    def test_single_consumer_last_row_tiles_the_producer(self):
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native driver required')
        result = subprocess.run([tool, '--verify-each', '--llm2fpga-fold-producer-slices',
                                 '--canonicalize'], input=self.fixture(), text=True,
                                capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('linalg.generic', result.stdout)
        self.assertIn('"reduction"', result.stdout)
        self.assertIn('tensor<1x3xi64>', result.stdout)
        self.assertNotIn('tensor<4x6xi64>', result.stdout)

    def test_constant_result_index_reduction_stays_unchanged(self):
        fixture = """module {
func.func @reduce(%x: tensor<4x3xi64>) -> tensor<1x1xi64> {
 %zero = arith.constant 0 : i64
 %empty = tensor.empty() : tensor<4x1xi64>
 %init = linalg.fill ins(%zero : i64) outs(%empty : tensor<4x1xi64>) -> tensor<4x1xi64>
 %full = linalg.generic {
 indexing_maps = [affine_map<(r,k)->(r,k)>, affine_map<(r,k)->(r,0)>],
 iterator_types = ["parallel", "reduction"]}
 ins(%x : tensor<4x3xi64>) outs(%init : tensor<4x1xi64>) {
 ^bb0(%a: i64, %b: i64):
 %square = arith.muli %a, %a : i64
 %sum = arith.addi %square, %b : i64
 linalg.yield %sum : i64
 } -> tensor<4x1xi64>
 %row = tensor.extract_slice %full[3,0] [1,1] [1,1] : tensor<4x1xi64> to tensor<1x1xi64>
 return %row : tensor<1x1xi64>
} }"""
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        result = subprocess.run([tool, '--verify-each', '--linalg-fuse-elementwise-ops', '--llm2fpga-fold-producer-slices'],
                                input=fixture, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tensor.extract_slice', result.stdout)
        self.assertIn('tensor<4x1xi64>', result.stdout)

    def test_selected_row_all_columns_execute_with_signed_wraparound(self):
        import tempfile
        import torch
        from compiler.pipeline import INTEGER_PROFILE
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        runner = os.environ['LLM2FPGA_MLIR_RUNNER']
        x = [[1, 2, 3], [-4, 5, -6], [7, -8, 9], [-(1 << 63), -3, 7]]
        weights = [[1, 0, 0], [-1, 1, 0], [3, -5, 7],
                   [0, -(1 << 63), 2], [(1 << 63)-1, 9, -11], [0, 0, 0]]
        def dense(rows):
            return '[' + ', '.join('[' + ', '.join(map(str, row)) + ']' for row in rows) + ']'
        passes = [arg for name, _, args, _ in INTEGER_PROFILE
                  if name in {'buffers', 'scratch', 'loops', 'structured'} for arg in args]
        for column in range(6):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as directory:
                source = self.fixture()[:-1] + f"""
func.func @main() -> i64 {{
 %x = arith.constant dense<{dense(x)}> : tensor<4x3xi64>
 %w = arith.constant dense<{dense(weights)}> : tensor<6x3xi64>
 %row = func.call @project(%x, %w) : (tensor<4x3xi64>, tensor<6x3xi64>) -> tensor<1x6xi64>
 %zero = arith.constant 0 : index
 %column = arith.constant {column} : index
 %value = tensor.extract %row[%zero, %column] : tensor<1x6xi64>
 return %value : i64
}} }}"""
                root = Path(directory)
                raw, llvm = root / 'source.mlir', root / 'llvm.mlir'
                raw.write_text(source)
                commands = [
                    [tool, str(raw), '--inline', *passes,
                     '--convert-scf-to-cf', '--convert-cf-to-llvm', '--convert-arith-to-llvm',
                     '--convert-index-to-llvm', '--convert-func-to-llvm',
                     '--finalize-memref-to-llvm', '--reconcile-unrealized-casts', '-o', str(llvm)],
                    [runner, str(llvm), '-e', 'main', '--entry-point-result=i64']]
                for command in commands:
                    result = subprocess.run(command, text=True, capture_output=True, timeout=120)
                    self.assertEqual(result.returncode, 0, result.stderr)
                # Expected values are evaluated only after compilation/execution.
                total = sum(a*b for a, b in zip(x[-1], weights[column]))
                expected = (total + (1 << 63)) % (1 << 64) - (1 << 63)
                self.assertEqual(int(result.stdout.strip()), expected)
                eager = torch.tensor(x, dtype=torch.int64) @ torch.tensor(weights, dtype=torch.int64).T
                self.assertEqual(int(result.stdout.strip()), int(eager[-1, column]))

    def test_multiple_consumers_preserve_full_producer(self):
        fixture = self.fixture().replace('-> tensor<1x6xi64> {', '-> (tensor<1x6xi64>, tensor<4x6xi64>) {').replace(
            'return %row : tensor<1x6xi64>',
            'return %row, %full : tensor<1x6xi64>, tensor<4x6xi64>')
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
                                 '--llm2fpga-fold-producer-slices'], input=fixture,
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tensor<4x6xi64>', result.stdout)
        self.assertIn('tensor.extract_slice', result.stdout)

    def test_strided_slice_preserves_full_producer(self):
        fixture = self.fixture().replace('tensor<1x6xi64>', 'tensor<2x6xi64>').replace(
            '%full[3,0] [1,6] [1,1]', '%full[0,0] [2,6] [2,1]')
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
                                 '--llm2fpga-fold-producer-slices'], input=fixture,
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tensor<4x6xi64>', result.stdout)
        self.assertIn('tensor.extract_slice', result.stdout)

    def test_full_size_slice_terminates_without_retiling(self):
        fixture = self.fixture().replace('tensor<1x6xi64>', 'tensor<4x6xi64>').replace(
            '%full[3,0] [1,6] [1,1]', '%full[0,0] [4,6] [1,1]')
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
                                 '--llm2fpga-fold-producer-slices'], input=fixture,
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tensor<4x6xi64>', result.stdout)
