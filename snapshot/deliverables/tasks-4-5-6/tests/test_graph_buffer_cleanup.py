"""The scratch boundary retires dead backing storage before lifetime analysis."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from compiler.pipeline import INTEGER_PROFILE


FIXTURE = '''module {
  func.func @main() -> i64 {
    %zero = arith.constant 0 : index
    %seven = arith.constant 7 : i64
    %backing = memref.alloc() : memref<4x13xi64>
    %unused = memref.subview %backing[3, 0] [1, 13] [1, 1]
      : memref<4x13xi64> to memref<1x13xi64, strided<[13, 1], offset: 39>>
    %also_unused = memref.subview %backing[3, 0] [1, 13] [1, 1]
      : memref<4x13xi64> to memref<1x13xi64, strided<[13, 1], offset: 39>>
    %live = memref.alloc() : memref<13xi64>
    linalg.fill ins(%seven : i64) outs(%live : memref<13xi64>)
    %value = memref.load %live[%zero] : memref<13xi64>
    return %value : i64
  }
}'''


class BufferCleanupTests(unittest.TestCase):
    def lower(self):
        arguments = next(args for name, _, args, _ in INTEGER_PROFILE
                         if name == 'scratch')
        result = subprocess.run(
            [os.environ['LLM2FPGA_TORCH_PASS_TOOL'], *arguments],
            input=FIXTURE, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_unused_backing_and_views_do_not_reach_scratch_planning(self):
        lowered = self.lower()
        self.assertNotIn('memref<4x13xi64>', lowered)
        self.assertNotIn('memref.subview', lowered)
        self.assertEqual(lowered.count('memref.alloc()'), 1)

    def test_live_storage_keeps_its_initialized_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'scratch.mlir', root / 'llvm.mlir'
            source.write_text(self.lower())
            commands = [
                [os.environ['LLM2FPGA_TORCH_PASS_TOOL'], str(source),
                 '--verify-each', '--convert-linalg-to-loops', '--lower-affine',
                 '--convert-scf-to-cf', '--convert-cf-to-llvm',
                 '--convert-arith-to-llvm', '--convert-index-to-llvm',
                 '--convert-func-to-llvm', '--finalize-memref-to-llvm',
                 '--reconcile-unrealized-casts', '-o', str(output)],
                [os.environ['LLM2FPGA_MLIR_RUNNER'], str(output), '-e', 'main',
                 '--entry-point-result=i64']]
            for command in commands:
                result = subprocess.run(command, text=True, capture_output=True,
                                        timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(int(result.stdout.strip()), 7)


if __name__ == '__main__':
    unittest.main()
