"""Native lowering carries graph origins onto reference-memory cells."""
import os
from pathlib import Path
import subprocess
import unittest

SOURCE = '''module {
 func.func @main(%weights: memref<3xi64> {llm2fpga.input_origin = {index = 0 : i64, kind = "BUFFER", target = "weights"}}, %output: memref<1xi64> {llm2fpga.runtime_port = "last_logits"}) {
  %c0 = arith.constant 0 : index
  %value = memref.load %weights[%c0] : memref<3xi64>
  memref.store %value, %output[%c0] : memref<1xi64>
  return
 }
}'''


class CalyxOriginTests(unittest.TestCase):
    def test_native_memory_reference_keeps_graph_origin(self):
        tool = os.environ['LLM2FPGA_CALYX_PASS_TOOL']
        result = subprocess.run([tool, '--verify-each', '--lower-scf-to-calyx'],
                                input=SOURCE, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines()
                 if 'calyx.seq_mem' in line and 'llm2fpga.input_origin' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('target = "weights"', lines[0])

    def test_native_memory_reference_keeps_runtime_role(self):
        result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--lower-scf-to-calyx'], input=SOURCE,
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines()
                 if 'calyx.seq_mem' in line and 'llm2fpga.runtime_port' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('last_logits', lines[0])


if __name__ == '__main__':
    unittest.main()
