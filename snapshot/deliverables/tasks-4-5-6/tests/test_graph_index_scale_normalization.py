"""Normalize index scales for sharing without changing modular arithmetic."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_graph_sequential_scratch as scratch_tests

PASS = '--llm2fpga-normalize-index-scales-for-sharing'


def shift_fixture(*, count=2, flags='', annotation='', scalar='index'):
    return f'''module {{
      func.func @scale(%x: {scalar}) -> {scalar} {{
        %shift = arith.constant {count} : {scalar}
        %result = arith.shli %x, %shift {flags} {annotation} : {scalar}
        return %result : {scalar}
      }}
    }}'''


class IndexScaleNormalizationTests(unittest.TestCase):
    def lower(self, source, *after):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', PASS, *after], input=source, text=True,
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_constant_index_scale_has_multiply_form_for_sharing(self):
        lowered = self.lower(shift_fixture())
        self.assertIn('arith.muli', lowered)
        self.assertNotIn('arith.shli', lowered)
        self.assertIn('arith.constant 4 : index', lowered)

    def test_dynamic_shift_and_out_of_contract_counts_are_preserved(self):
        dynamic = '''module { func.func @scale(%x: index, %n: index) -> index {
          %result = arith.shli %x, %n : index
          return %result : index
        }}'''
        for source in (dynamic, shift_fixture(count=-1), shift_fixture(count=31),
                       shift_fixture(count=32), shift_fixture(count=63)):
            with self.subTest(source=source):
                self.assertIn('arith.shli', self.lower(source))

    def test_nonindex_arithmetic_and_extra_contracts_are_preserved(self):
        for source in (shift_fixture(scalar='i64'),
                       shift_fixture(flags='overflow<nsw>'),
                       shift_fixture(annotation='{llm2fpga.test = "preserve"}')):
            with self.subTest(source=source):
                self.assertIn('arith.shli', self.lower(source))

    def test_negative_and_wrapping_values_execute_exactly(self):
        executor = scratch_tests.SequentialScratchTests()
        for value, shift, expected in ((-3, 2, -12), (7, 0, 7),
                                      (9223372036854775807, 2, -4),
                                      (-9223372036854775808, 1, 0),
                                      (3, 30, 3221225472)):
            source = f'''module {{ func.func @main() -> i64 {{
              %x = arith.constant {value} : index
              %n = arith.constant {shift} : index
              %scaled = arith.shli %x, %n : index
              %result = arith.index_cast %scaled : index to i64
              return %result : i64
            }} }}'''
            with self.subTest(value=value, shift=shift):
                self.assertEqual(executor.execute(source), expected)
                self.assertEqual(executor.execute(self.lower(source)), expected)

    def test_shift_and_multiply_helpers_share_and_keep_literal_results(self):
        source = '''module {
          func.func private @a(%x: index, %out: memref<1xi64>) {
            %zero = arith.constant 0 : index
            %two = arith.constant 2 : index
            %scaled = arith.shli %x, %two : index
            %value = arith.index_cast %scaled : index to i64
            memref.store %value, %out[%zero] : memref<1xi64>
            return
          }
          func.func private @b(%x: index, %out: memref<1xi64>) {
            %zero = arith.constant 0 : index
            %five = arith.constant 5 : index
            %scaled = arith.muli %x, %five : index
            %value = arith.index_cast %scaled : index to i64
            memref.store %value, %out[%zero] : memref<1xi64>
            return
          }
          func.func @main() -> i64 {
            %zero = arith.constant 0 : index
            %x = arith.constant -3 : index
            %a = memref.alloc() : memref<1xi64>
            %b = memref.alloc() : memref<1xi64>
            func.call @a(%x, %a) : (index, memref<1xi64>) -> ()
            func.call @b(%x, %b) : (index, memref<1xi64>) -> ()
            %left = memref.load %a[%zero] : memref<1xi64>
            %right = memref.load %b[%zero] : memref<1xi64>
            %result = arith.addi %left, %right : i64
            return %result : i64
          }
        }'''
        lowered = self.lower(source, '--llm2fpga-share-private-functions=entrypoint=main')
        self.assertEqual(len(re.findall(r'func.func private @', lowered)), 1)
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source), -27)
        self.assertEqual(executor.execute(lowered), -27)

    def test_normalized_index_scale_can_be_strength_reduced_after_sharing(self):
        lowered = self.lower(shift_fixture(), '--llm2fpga-strength-reduce-integer-constants=index-scales=true')
        self.assertIn('arith.shli', lowered)
        self.assertNotIn('arith.muli', lowered)

    def test_existing_strength_pass_restores_only_unflagged_index_powers_of_two(self):
        for scalar, factor, flags, expected_op in (
                ('index', 4, '', 'arith.shli'),
                ('index', 1073741824, '', 'arith.shli'),
                ('index', 5, '', 'arith.muli'),
                ('index', 2147483648, '', 'arith.muli'),
                ('index', 4, 'overflow<nsw>', 'arith.muli'),
                ('i64', 4, '', 'arith.muli')):
            source = f'''module {{ func.func @scale(%x: {scalar}) -> {scalar} {{
              %factor = arith.constant {factor} : {scalar}
              %result = arith.muli %x, %factor {flags} : {scalar}
              return %result : {scalar}
            }} }}'''
            with self.subTest(scalar=scalar, factor=factor, flags=flags):
                result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                    '--verify-each', '--llm2fpga-strength-reduce-integer-constants=index-scales=true'],
                    input=source, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected_op, result.stdout)

    def test_default_strength_order_keeps_index_specializations_shareable(self):
        source = '''module {
          func.func private @a(%x:index, %out:memref<1xi64>) {
            %i=arith.constant 0:index
            %factor=arith.constant 4:index
            %scaled=arith.muli %x,%factor:index
            %value=arith.index_cast %scaled:index to i64
            memref.store %value,%out[%i]:memref<1xi64>
            return
          }
          func.func private @b(%x:index, %out:memref<1xi64>) {
            %i=arith.constant 0:index
            %factor=arith.constant 5:index
            %scaled=arith.muli %x,%factor:index
            %value=arith.index_cast %scaled:index to i64
            memref.store %value,%out[%i]:memref<1xi64>
            return
          }
          func.func @main(%x:index,%out:memref<1xi64>) {
            func.call @a(%x,%out):(index,memref<1xi64>)->()
            func.call @b(%x,%out):(index,memref<1xi64>)->()
            return
          }
        }'''
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--llm2fpga-strength-reduce-integer-constants',
            '--int-range-optimizations', '--canonicalize',
            '--llm2fpga-share-private-functions=entrypoint=main'],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(re.findall(r'func.func private @', result.stdout)), 1)

    def test_forward_and_restored_arithmetic_execute_at_both_index_widths(self):
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        for width in (32, 64):
            for value, count, expected32, expected64 in (
                    (2147483647, 2, -4, 8589934588),
                    (-2147483648, 1, 0, -4294967296),
                    (-3, 2, -12, -12),
                    (3, 30, -1073741824, 3221225472)):
                source = f'''module {{ func.func @main() -> i64 {{
                  %x=arith.constant {value}:index
                  %n=arith.constant {count}:index
                  %y=arith.shli %x,%n:index
                  %r=arith.index_cast %y:index to i64
                  return %r:i64
                }} }}'''
                for restored in (False, True):
                    with self.subTest(width=width, value=value, count=count, restored=restored):
                        after = ('--llm2fpga-strength-reduce-integer-constants=index-scales=true',) if restored else ()
                        candidate = self.lower(source, *after)
                        with tempfile.TemporaryDirectory() as directory:
                            llvm = Path(directory) / 'llvm.mlir'
                            result = subprocess.run([tool,
                                f'--convert-arith-to-llvm=index-bitwidth={width}',
                                f'--convert-index-to-llvm=index-bitwidth={width}',
                                f'--convert-func-to-llvm=index-bitwidth={width}',
                                '--reconcile-unrealized-casts', '-o', str(llvm)],
                                input=candidate, text=True, capture_output=True, timeout=30)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            executed = subprocess.run([os.environ['LLM2FPGA_MLIR_RUNNER'],
                                str(llvm), '-e', 'main', '--entry-point-result=i64'],
                                text=True, capture_output=True, timeout=30)
                            self.assertEqual(executed.returncode, 0, executed.stderr)
                            self.assertEqual(int(executed.stdout.strip()), expected32 if width == 32 else expected64)
