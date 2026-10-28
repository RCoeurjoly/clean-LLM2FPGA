"""Bounded lowering of a typed loop workload, with verification enabled."""
import os
from pathlib import Path
import subprocess
import unittest


def loop_module(count):
    loop = """
    %unused{ordinal} = scf.while (%index = %zero) : (index) -> index {{
      %continue = arith.cmpi slt, %index, %one : index
      scf.condition(%continue) %index : index
    }} do {{
    ^bb0(%index: index):
      %next = arith.addi %index, %one : index
      memref.store %seven, %output[%index] : memref<1xi64>
      scf.yield %next : index
    }}
    """
    return """module {
  func.func @main(%output: memref<1xi64>) {
    %zero = arith.constant 0 : index
    %one = arith.constant 1 : index
    %seven = arith.constant 7 : i64
    """ + ''.join(loop.format(ordinal=i) for i in range(count)) + "return\n}}"


class CalyxLoweringScaleTests(unittest.TestCase):
    def test_many_memories_lower_with_bounded_port_lookup_work(self):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx tool required')
        count = 12000
        stores = ''.join(
            f'%memory{i} = memref.alloc() : memref<1xi64>\n'
            f'memref.store %seven, %memory{i}[%zero] : memref<1xi64>\n'
            for i in range(count))
        source = 'module { func.func @main() {\n' + (
            '%zero = arith.constant 0 : index\n'
            '%seven = arith.constant 7 : i64\n') + stores + 'return\n}}'
        result = subprocess.run(
            [tool, '--verify-each', '--lower-scf-to-calyx'],
            input=source, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('calyx.seq_mem '), count)

    def test_unsupported_loop_reports_diagnostic_without_dialect_crash(self):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx tool required')
        source = """module {
  func.func @main(%value: i32) {
    %zero = arith.constant 0 : index
    %one = arith.constant 1 : index
    %output = memref.alloc() : memref<1xi32>
    // expected-error @+1 {{AffineParallelUnroll must be run in order to lower scf.parallel}}
    scf.parallel (%i) = (%zero) to (%one) step (%one) {
      scf.execute_region {
        memref.store %value, %output[%i] : memref<1xi32>
        scf.yield
      }
    }
    return
  }
}"""
        result = subprocess.run(
            [tool, '--verify-each', '--lower-scf-to-calyx', '--verify-diagnostics'],
            input=source, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_many_loops_lower_with_bounded_control_scans(self):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx tool required')
        result = subprocess.run(
            [tool, '--verify-each', '--lower-scf-to-calyx'],
            input=loop_module(6400), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('calyx.component @main', result.stdout)
        self.assertEqual(result.stdout.count('calyx.while '), 6400)
