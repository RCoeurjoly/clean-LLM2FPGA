"""Native SCF scalar call operands must survive Calyx/Futil serialization."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SOURCE = '''module {
  func.func @worker(%out: memref<1xi32>, %value: i32) {
    %zero = arith.constant 0 : index
    memref.store %value, %out[%zero] : memref<1xi32>
    return
  }
  func.func @main(%out: memref<1xi32>) {
    %value = arith.constant 37 : i32
    func.call @worker(%out, %value) : (memref<1xi32>, i32) -> ()
    return
  }
}
'''


class ScalarCallTests(unittest.TestCase):
    def emit(self, source=SOURCE):
        opt = os.environ['LLM2FPGA_CALYX_PASS_TOOL']
        translator = os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL']
        with tempfile.TemporaryDirectory() as temporary:
            bytecode = Path(temporary)/'callee.mlirbc'
            lowered = subprocess.run([opt, '--verify-each',
                '--lower-scf-to-calyx=top-level-function=main',
                '--llm2fpga-complete-calyx-memory-contracts',
                '--emit-bytecode', '-o', str(bytecode)], input=source,
                text=True, capture_output=True, timeout=30)
            self.assertEqual(lowered.returncode, 0, lowered.stderr)
            emitted = subprocess.run([translator, str(bytecode), '--export-calyx'],
                text=True, capture_output=True, timeout=30)
        self.assertEqual(emitted.returncode, 0, emitted.stderr)
        return emitted.stdout

    def call(self, source=SOURCE):
        text = self.emit(source)
        calls = [line for line in text.splitlines() if 'invoke worker' in line]
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_scalar_call_survives_native_lowering_and_export(self):
        self.assertIn("in1 = 32'd37", self.call(),
                      'scalar operand was dropped from the emitted invocation')

    def test_runtime_component_argument_is_forwarded(self):
        source = SOURCE.replace('@main(%out: memref<1xi32>)',
                                '@main(%out: memref<1xi32>, %value: i32)')
        source = source.replace('    %value = arith.constant 37 : i32\n', '')
        self.assertIn('in1 = in1', self.call(source))

    def test_negative_constant_uses_exact_width_binary_literal(self):
        source = SOURCE.replace('arith.constant 37', 'arith.constant -7')
        self.assertIn("in1 = 32'b" + format((-7) & 0xffffffff, '032b'),
                      self.call(source))


if __name__ == '__main__':
    unittest.main()
