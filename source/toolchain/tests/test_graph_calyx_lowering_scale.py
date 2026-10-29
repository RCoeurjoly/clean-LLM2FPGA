"""Bounded lowering of a typed loop workload, with verification enabled."""
import os
from pathlib import Path
import subprocess
import unittest


class CalyxLoweringScaleTests(unittest.TestCase):

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
