"""One-trip loops must disappear before conversion to hardware while control."""
import os
import subprocess
import unittest
from compiler.pipeline import INTEGER_PROFILE

SOURCE = '''module {
func.func @main(%values: memref<1xi64>) attributes {llvm.emit_c_interface} {
 %zero = arith.constant 0 : index
 %one = arith.constant 1 : index
 %delta = arith.constant 1 : i64
 scf.for %i = %zero to %one step %one {
  %value = memref.load %values[%i] : memref<1xi64>
  %next = arith.addi %value, %delta : i64
  memref.store %next, %values[%i] : memref<1xi64>
 }
 return
} }'''


class OneTripLoopTests(unittest.TestCase):
    def lower(self, extra=()):
        args = next(args for name, _, args, _ in INTEGER_PROFILE if name == 'structured')
        return subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], *args, *extra],
            input=SOURCE, text=True, capture_output=True, timeout=120)

    def test_one_trip_loop_does_not_allocate_while_control(self):
        result = self.lower()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('scf.while', result.stdout)
        self.assertNotIn('scf.for', result.stdout)
        self.assertEqual(result.stdout.count('memref.store'), 1)

    def test_one_trip_memory_effect_executes_once_with_wrapping_extremes(self):
        import ctypes
        import numpy as np
        from torch_mlir import ir
        from torch_mlir.execution_engine import ExecutionEngine
        from torch_mlir.runtime import get_ranked_memref_descriptor
        extra = ('--convert-scf-to-cf', '--convert-cf-to-llvm', '--convert-arith-to-llvm',
                 '--convert-index-to-llvm', '--convert-func-to-llvm',
                 '--finalize-memref-to-llvm', '--reconcile-unrealized-casts')
        result = self.lower(extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        with ir.Context():
            engine = ExecutionEngine(ir.Module.parse(result.stdout))
            for value in (-(1 << 63), -1, 0, (1 << 63)-1):
                data = np.array([value], dtype=np.int64)
                descriptor = get_ranked_memref_descriptor(data)
                engine.invoke('main', ctypes.pointer(ctypes.pointer(descriptor)))
                expected = (value + 1 + (1 << 63)) % (1 << 64) - (1 << 63)
                self.assertEqual(int(data[0]), expected)
