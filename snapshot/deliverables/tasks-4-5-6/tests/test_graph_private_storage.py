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
        self.assertIn('memref<1x6xi64>', lowered)
        self.assertNotIn('memref<4x6xi64>', lowered)
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(FIXTURE), 7)
        self.assertEqual(executor.execute(lowered), 7)

    def test_multiple_fixed_dimensions_compact(self):
        source = FIXTURE.replace('4x6', '4x5x6').replace(
            '[%three, %i]', '[%three, %two, %i]').replace(
            '[%three, %two]', '[%three, %two, %two]')
        self.assertIn('memref<1x1x6xi64>', self.compact(source))

    def test_loop_range_proves_derived_fixed_row(self):
        from compiler.pipeline import INTEGER_PROFILE
        source = FIXTURE.replace('memref.store %seven', '''%row = arith.divui %i, %six : index
            %offset = arith.addi %row, %three : index
            memref.store %seven''').replace('[%three, %i]', '[%offset, %i]')
        arguments = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'loops')
        result = self.run_pass(source, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('memref<1x6xi64>', result.stdout)
        self.assertEqual(scratch_tests.SequentialScratchTests().execute(result.stdout), 7)

    def test_different_rows_are_preserved(self):
        source = FIXTURE.replace('memref.load %array[%three', 'memref.load %array[%two')
        self.assertIn('memref<4x6xi64>', self.compact(source))

    def test_shape_observer_prevents_compaction(self):
        source = FIXTURE.replace('scf.for',
            '%size = memref.dim %array, %zero : memref<4x6xi64>\n    scf.for')
        self.assertIn('memref<4x6xi64>', self.compact(source))

    def test_alias_prevents_compaction(self):
        source = FIXTURE.replace('scf.for', '''%view = memref.subview %array[3, 0] [1, 6] [1, 1]
          : memref<4x6xi64> to memref<1x6xi64, strided<[6, 1], offset: 18>>
        scf.for''')
        self.assertIn('memref<4x6xi64>', self.compact(source))

    def test_unknown_annotation_prevents_compaction(self):
        source = FIXTURE.replace('memref.alloc()',
                                'memref.alloc() {llm2fpga.test = "preserve"}')
        self.assertIn('memref<4x6xi64>', self.compact(source))

    def test_invalid_fixed_index_is_not_repaired(self):
        source = FIXTURE.replace('constant 3 : index', 'constant 4 : index')
        self.assertIn('memref<4x6xi64>', self.compact(source))

    def test_functional_inputs_are_readonly_before_native_analysis(self):
        source = '''module {
          func.func @update(%input: tensor<4xi64>) -> tensor<4xi64> {
            %one = arith.constant 1 : i64
            %updated = linalg.generic {
              indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>],
              iterator_types = ["parallel"]}
              ins(%input : tensor<4xi64>) outs(%input : tensor<4xi64>) {
              ^bb0(%value: i64, %old: i64):
                %next = arith.addi %value, %one : i64
                linalg.yield %next : i64
            } -> tensor<4xi64>
            return %updated : tensor<4xi64>
          }
        }'''
        prepared = self.run_pass(source, '--llm2fpga-protect-functional-inputs')
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertIn('bufferization.writable = false', prepared.stdout)
        buffered = self.run_pass(prepared.stdout,
            '--one-shot-bufferize=bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map')
        self.assertEqual(buffered.returncode, 0, buffered.stderr)
        self.assertIn('memref.alloc()', buffered.stdout)

    def test_conflicting_writable_input_contract_is_rejected(self):
        source = '''module {
          func.func @identity(%x: tensor<4xi64> {bufferization.writable = true})
              -> tensor<4xi64> { return %x : tensor<4xi64> }
        }'''
        result = self.run_pass(source, '--llm2fpga-protect-functional-inputs')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('functional input contract', result.stderr)

    def test_common_profile_declares_native_analysis_and_compaction(self):
        from compiler.pipeline import INTEGER_PROFILE
        buffers = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'buffers')
        self.assertIn('--llm2fpga-protect-functional-inputs', buffers)
        self.assertNotIn('copy-before-write', ' '.join(buffers))
        loops = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'loops')
        self.assertIn('--int-range-optimizations', loops)
        self.assertEqual(loops[-2:], ('--canonicalize', '--llm2fpga-compact-fixed-dim-storage'))


if __name__ == '__main__':
    unittest.main()
