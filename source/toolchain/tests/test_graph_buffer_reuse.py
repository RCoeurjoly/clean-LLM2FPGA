"""Scratch storage sharing requires disjoint lifetimes and full initialization."""
import os
from pathlib import Path
import re
import subprocess
import unittest


def fixture(body):
    return '''module {
      func.func @main(%out: memref<4xi64>) {
        %zero = arith.constant 0 : i64
        %one = arith.constant 1 : i64
        %idx = arith.constant 0 : index
        ''' + body + '\nreturn\n}\n}\n'


class BufferReuseTests(unittest.TestCase):
    def lower(self, body):
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Torch pass tool required')
        result = subprocess.run([tool, '--verify-each', '--llm2fpga-reuse-buffers'],
                                input=fixture(body), text=True, capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def allocations(self, body):
        return len(re.findall(r' = memref.alloc\(', self.lower(body)))

    def test_disjoint_fully_initialized_scratch_reuses_storage(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          memref.copy %a, %out : memref<4xi64> to memref<4xi64>
          %b = memref.alloc() : memref<4xi64>
          linalg.fill ins(%one : i64) outs(%b : memref<4xi64>)
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 1)

    def test_overlapping_inputs_and_outputs_do_not_alias(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          %b = memref.alloc() : memref<4xi64>
          memref.copy %a, %b : memref<4xi64> to memref<4xi64>
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 2)

    def test_partial_initialization_does_not_reuse_old_contents(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          memref.copy %a, %out : memref<4xi64> to memref<4xi64>
          %b = memref.alloc() : memref<4xi64>
          memref.store %one, %b[%idx] : memref<4xi64>
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 2)

    def test_aliasing_view_is_conservatively_excluded(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          %v = memref.subview %a[0] [4] [1] : memref<4xi64> to memref<4xi64, strided<[1]>>
          memref.copy %v, %out : memref<4xi64, strided<[1]>> to memref<4xi64>
          %b = memref.alloc() : memref<4xi64>
          linalg.fill ins(%one : i64) outs(%b : memref<4xi64>)
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 2)

    def test_full_elementwise_write_can_reuse_storage(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          memref.copy %a, %out : memref<4xi64> to memref<4xi64>
          %b = memref.alloc() : memref<4xi64>
          linalg.generic {
            indexing_maps = [affine_map<(d0) -> (d0)>, affine_map<(d0) -> (d0)>],
            iterator_types = ["parallel"]
          } ins(%out : memref<4xi64>) outs(%b : memref<4xi64>) {
          ^bb0(%input: i64, %old: i64):
            %value = arith.addi %input, %one : i64
            linalg.yield %value : i64
          }
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 1)

    def test_elementwise_accumulation_needs_old_contents(self):
        self.assertEqual(self.allocations('''
          %a = memref.alloc() : memref<4xi64>
          linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
          memref.copy %a, %out : memref<4xi64> to memref<4xi64>
          %b = memref.alloc() : memref<4xi64>
          linalg.generic {
            indexing_maps = [affine_map<(d0) -> (d0)>, affine_map<(d0) -> (d0)>],
            iterator_types = ["parallel"]
          } ins(%out : memref<4xi64>) outs(%b : memref<4xi64>) {
          ^bb0(%input: i64, %old: i64):
            %value = arith.addi %input, %old : i64
            linalg.yield %value : i64
          }
          memref.copy %b, %out : memref<4xi64> to memref<4xi64>
        '''), 2)


if __name__ == '__main__':
    unittest.main()
