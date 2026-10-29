"""Compact only unobserved private storage dimensions with fixed access indices."""
import os
import subprocess
import unittest

import test_graph_sequential_scratch as scratch_tests


FIXTURE = '''module {
  func.func @main() -> i64 {
    %zero = arith.constant 0 : index
    %one = arith.constant 1 : index
    %two = arith.constant 2 : index
    %three = arith.constant 3 : index
    %six = arith.constant 6 : index
    %seven = arith.constant 7 : i64
    %array = memref.alloc() : memref<4x6xi64>
    scf.for %i = %zero to %six step %one {
      memref.store %seven, %array[%three, %i] : memref<4x6xi64>
    }
    %result = memref.load %array[%three, %two] : memref<4x6xi64>
    return %result : i64
  }
}'''


class PrivateStorageTests(unittest.TestCase):
    def run_pass(self, source, *arguments):
        return subprocess.run(
            [os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each', *arguments],
            input=source, text=True, capture_output=True, timeout=30)

    def compact(self, source):
        result = self.run_pass(source, '--llm2fpga-compact-fixed-dim-storage')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_fixed_row_storage_compacts_without_changing_values(self):
        lowered = self.compact(FIXTURE)
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(FIXTURE), 7)
        self.assertEqual(executor.execute(lowered), 7)


    def test_loop_range_proves_derived_fixed_row(self):
        from compiler.pipeline import INTEGER_PROFILE
        source = FIXTURE.replace('memref.store %seven', '''%row = arith.divui %i, %six : index
            %offset = arith.addi %row, %three : index
            memref.store %seven''').replace('[%three, %i]', '[%offset, %i]')
        arguments = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'loops')
        result = self.run_pass(source, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(scratch_tests.SequentialScratchTests().execute(result.stdout), 7)


    def test_conflicting_writable_input_contract_is_rejected(self):
        source = '''module {
          func.func @identity(%x: tensor<4xi64> {bufferization.writable = true})
              -> tensor<4xi64> { return %x : tensor<4xi64> }
        }'''
        result = self.run_pass(source, '--llm2fpga-protect-functional-inputs')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('functional input contract', result.stderr)


if __name__ == '__main__':
    unittest.main()
