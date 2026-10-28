"""Native fixtures require a selected built driver; absence is a failure."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class NativeIntegerTests(unittest.TestCase):
    def execute(self, source, torch_passes=None):
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        runner = os.environ.get('LLM2FPGA_MLIR_RUNNER')
        if not tool or not runner:
            self.fail('native driver and pinned MLIR runner are required')
        with tempfile.TemporaryDirectory() as directory:
            standard = Path(directory) / 'standard.mlir'
            llvm = Path(directory) / 'llvm.mlir'
            first = subprocess.run([tool,
                '--pass-pipeline=' + (torch_passes or 'builtin.module(llm2fpga-lower-exact-integer-ops,llm2fpga-lower-serial-gemv,canonicalize,torch-backend-to-linalg-on-tensors-backend-pipeline)'),
                '-o', str(standard)], input=source, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = subprocess.run([tool, str(standard),
                '--one-shot-bufferize=bufferize-function-boundaries', '--convert-linalg-to-loops',
                '--lower-affine', '--convert-scf-to-cf', '--convert-cf-to-llvm',
                '--convert-arith-to-llvm', '--convert-index-to-llvm', '--convert-func-to-llvm',
                '--finalize-memref-to-llvm', '--reconcile-unrealized-casts', '-o', str(llvm)],
                text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            result = subprocess.run([runner, str(llvm), '-e', 'main', '--entry-point-result=i64'],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return int(result.stdout.strip())

    def run_pass(self, source, name='llm2fpga-lower-serial-gemv'):
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        if not tool or not Path(tool).is_file():
            self.fail('LLM2FPGA_TORCH_PASS_TOOL must select the built native driver')
        return subprocess.run([tool, '--pass-pipeline=builtin.module(' + name + ')'],
                              input=source, text=True, capture_output=True)

    def fixture(self, lhs='[1,3],si64', rhs='[2,3],si64', result='[1,2],si64', operator='torch.llm2fpga.serial_gemv'):
        return f'''module {{
  func.func @gemv(%x: !torch.vtensor<{lhs}>, %w: !torch.vtensor<{rhs}>) -> !torch.vtensor<{result}> {{
    %r = torch.operator "{operator}"(%x, %w) : (!torch.vtensor<{lhs}>, !torch.vtensor<{rhs}>) -> !torch.vtensor<{result}>
    return %r : !torch.vtensor<{result}>
  }}
}}'''

    def test_gemv_is_real_reduction(self):
        result = self.run_pass(self.fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('linalg.generic', result.stdout)
        self.assertIn('reduction', result.stdout)
        self.assertIn('arith.muli', result.stdout)
        self.assertIn('arith.addi', result.stdout)
        self.assertNotIn('torch.operator', result.stdout)
        self.assertNotIn('llm2fpga.serial_gemv', result.stdout)

    def test_standard_integer_boundary_rejects_float_and_torch(self):
        for source in ('module { func.func @f(%x: f64) -> f64 { return %x : f64 } }',
                       self.fixture()):
            with self.subTest(source=source):
                result = self.run_pass(source, 'llm2fpga-check-standard-integer-ir')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('standard_integer_contract', result.stderr)
        result = self.run_pass('module { func.func @f(%x: i64) -> i64 { return %x : i64 } }',
                               'llm2fpga-check-standard-integer-ir')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scalar_test_entry_uses_tensor_value(self):
        source = '''module { func.func @main() -> tensor<i64> {
 %x = arith.constant dense<7> : tensor<i64>
 return %x : tensor<i64>
} }'''
        result = self.run_pass(source, 'llm2fpga-test-scalar-entry')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('tensor.extract', result.stdout)
        self.assertIn('-> i64', result.stdout)

    def test_gemv_contract_failures(self):
        for source in (self.fixture(lhs='[3],si64'), self.fixture(rhs='[2,4],si64'),
                       self.fixture(lhs='[1,3],f32'), self.fixture(operator='torch.unknown')):
            with self.subTest(source=source):
                result = self.run_pass(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('serial_gemv_contract', result.stderr)

    def test_signed_shift_is_arithmetic(self):
        source = '''module {
 func.func @shift(%x: !torch.vtensor<[4],si64>) -> !torch.vtensor<[4],si64> {
  %s = torch.constant.int 1
  %r = torch.operator "torch.aten.bitwise_right_shift.Tensor_Scalar"(%x, %s) : (!torch.vtensor<[4],si64>, !torch.int) -> !torch.vtensor<[4],si64>
  return %r : !torch.vtensor<[4],si64>
 }
}'''
        result = self.run_pass(source, 'llm2fpga-lower-exact-integer-ops')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.shrsi', result.stdout)
        self.assertNotIn('torch.operator', result.stdout)

    def test_signed_extremes_execute_after_lowering(self):
        values = [-9223372036854775808, -3, 3, 9223372036854775807]
        for shift in (0, 1, 16, 32, 62):
            for index, value in enumerate(values):
                with self.subTest(shift=shift, value=value):
                    source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[{','.join(map(str, values))}]> : tensor<4xi64>
  %t = torch_c.from_builtin_tensor %x : tensor<4xi64> -> !torch.vtensor<[4],si64>
  %s = torch.constant.int {shift}
  %r = torch.operator "torch.aten.bitwise_right_shift.Tensor_Scalar"(%t, %s) : (!torch.vtensor<[4],si64>, !torch.int) -> !torch.vtensor<[4],si64>
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[4],si64> -> tensor<4xi64>
  %i = arith.constant {index} : index
  %value = tensor.extract %b[%i] : tensor<4xi64>
  return %value : i64
 }}
}}'''
                    self.assertEqual(self.execute(source), value >> shift)

    def test_wrapping_mac_executes_after_lowering(self):
        values, weights = [9223372036854775807, -9223372036854775808, -3], [2, -1, 7]
        source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[[{','.join(map(str, values))}]]> : tensor<1x3xi64>
  %w = arith.constant dense<[[{','.join(map(str, weights))}]]> : tensor<1x3xi64>
  %xt = torch_c.from_builtin_tensor %x : tensor<1x3xi64> -> !torch.vtensor<[1,3],si64>
  %wt = torch_c.from_builtin_tensor %w : tensor<1x3xi64> -> !torch.vtensor<[1,3],si64>
  %r = torch.operator "torch.llm2fpga.serial_gemv"(%xt, %wt) : (!torch.vtensor<[1,3],si64>, !torch.vtensor<[1,3],si64>) -> !torch.vtensor<[1,1],si64>
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[1,1],si64> -> tensor<1x1xi64>
  %i = arith.constant 0 : index
  %value = tensor.extract %b[%i,%i] : tensor<1x1xi64>
  return %value : i64
 }}
}}'''
        expected = sum(x * w for x, w in zip(values, weights))
        expected = (expected + (1 << 63)) % (1 << 64) - (1 << 63)
        self.assertEqual(self.execute(source), expected)

    def test_floor_and_truncation_execute_after_lowering(self):
        values = [-9223372036854775808, -3, 3, 9223372036854775807]
        for mode in ('floor', 'trunc'):
            for index, value in enumerate(values):
                with self.subTest(mode=mode, value=value):
                    source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[{','.join(map(str, values))}]> : tensor<4xi64>
  %d = arith.constant dense<2> : tensor<4xi64>
  %t = torch_c.from_builtin_tensor %x : tensor<4xi64> -> !torch.vtensor<[4],si64>
  %dt = torch_c.from_builtin_tensor %d : tensor<4xi64> -> !torch.vtensor<[4],si64>
  %mode = torch.constant.str "{mode}"
  %r = torch.aten.div.Tensor_mode %t, %dt, %mode : !torch.vtensor<[4],si64>, !torch.vtensor<[4],si64>, !torch.str -> !torch.vtensor<[4],si64>
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[4],si64> -> tensor<4xi64>
  %i = arith.constant {index} : index
  %value = tensor.extract %b[%i] : tensor<4xi64>
  return %value : i64
 }}
}}'''
                    expected = value // 2 if mode == 'floor' else (-1 if value < 0 else 1) * (abs(value) // 2)
                    self.assertEqual(self.execute(source), expected)

    def test_argmax_signed_extremes_and_first_tie_execute_after_lowering(self):
        from compiler.pipeline import TORCH_INTEGER_PASSES
        minimum, maximum = -(1 << 63), (1 << 63) - 1
        for values, wanted in (([minimum, minimum, minimum], 0),
                               ([maximum, minimum, maximum], 0),
                               ([minimum, maximum, maximum], 1),
                               ([-9, -3, -3], 1), ([0, 0, 0], 0)):
            with self.subTest(values=values):
                source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[{','.join(map(str, values))}]> : tensor<3xi64>
  %t = torch_c.from_builtin_tensor %x : tensor<3xi64> -> !torch.vtensor<[3],si64>
  %dim = torch.constant.none
  %keep = torch.constant.bool false
  %r = torch.aten.argmax %t, %dim, %keep : !torch.vtensor<[3],si64>, !torch.none, !torch.bool -> !torch.vtensor<[],si64>
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[],si64> -> tensor<i64>
  %value = tensor.extract %b[] : tensor<i64>
  return %value : i64
 }}
}}'''
                self.assertEqual(self.execute(source, TORCH_INTEGER_PASSES), wanted)

    def test_signed_remainder_execute_after_lowering(self):
        values = [-9223372036854775808, -3, 3, 9223372036854775807]
        for index, value in enumerate(values):
            with self.subTest(value=value):
                source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[{','.join(map(str, values))}]> : tensor<4xi64>
  %t = torch_c.from_builtin_tensor %x : tensor<4xi64> -> !torch.vtensor<[4],si64>
  %d = torch.constant.int 256
  %r = torch.aten.remainder.Scalar %t, %d : !torch.vtensor<[4],si64>, !torch.int -> !torch.vtensor<[4],si64>
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[4],si64> -> tensor<4xi64>
  %i = arith.constant {index} : index
  %value = tensor.extract %b[%i] : tensor<4xi64>
  return %value : i64
 }}
}}'''
                self.assertEqual(self.execute(source), value % 256)

    def test_negation_and_saturation_execute_after_lowering(self):
        values = [-9223372036854775808, -129, -128, 127, 128, 9223372036854775807]
        for operation in ('neg', 'clamp'):
            for index, value in enumerate(values):
                body = ('%r = torch.aten.neg %t : !torch.vtensor<[6],si64> -> !torch.vtensor<[6],si64>'
                        if operation == 'neg' else '''%lo = torch.constant.int -128
  %hi = torch.constant.int 127
  %r = torch.aten.clamp %t, %lo, %hi : !torch.vtensor<[6],si64>, !torch.int, !torch.int -> !torch.vtensor<[6],si64>''')
                with self.subTest(operation=operation, value=value):
                    source = f'''module {{
 func.func @main() -> i64 {{
  %x = arith.constant dense<[{','.join(map(str, values))}]> : tensor<6xi64>
  %t = torch_c.from_builtin_tensor %x : tensor<6xi64> -> !torch.vtensor<[6],si64>
  {body}
  %b = torch_c.to_builtin_tensor %r : !torch.vtensor<[6],si64> -> tensor<6xi64>
  %i = arith.constant {index} : index
  %value = tensor.extract %b[%i] : tensor<6xi64>
  return %value : i64
 }}
}}'''
                    expected = ((-value + (1 << 63)) % (1 << 64) - (1 << 63)
                                if operation == 'neg' else max(-128, min(127, value)))
                    self.assertEqual(self.execute(source), expected)

    def test_integer_abs_preserves_wrapping_minimum(self):
        for value in (0, -1, -7, -(1 << 63), (1 << 63) - 1):
            source = f"module {{ func.func @main() -> i64 {{ %x = arith.constant {value} : i64\n %r = math.absi %x : i64\n return %r : i64 }} }}"
            with self.subTest(value=value):
                lowered = self.run_pass(source, 'llm2fpga-lower-integer-math')
                self.assertEqual(lowered.returncode, 0, lowered.stderr)
                self.assertNotIn('math.absi', lowered.stdout)
                expected = value if value == -(1 << 63) else abs(value)
                self.assertEqual(self.execute(lowered.stdout), expected)

    def test_integer_math_rejects_unlowered_vector_abs(self):
        source = "module { func.func @f(%x: vector<2xi64>) -> vector<2xi64> { %r = math.absi %x : vector<2xi64>\n return %r : vector<2xi64> } }"
        result = self.run_pass(source, 'llm2fpga-lower-integer-math')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('integer_math_contract', result.stderr)

    def test_power_two_division_rewrites_typed_scalar_operations(self):
        source = "module { func.func @f(%x: i64) -> (i64, i64) { %d = arith.constant 8 : i64\n %q = arith.divsi %x, %d : i64\n %r = arith.remsi %x, %d : i64\n return %q, %r : i64, i64 } }"
        result = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('arith.divsi', result.stdout)
        self.assertNotIn('arith.remsi', result.stdout)
        self.assertIn('arith.shrsi', result.stdout)
        self.assertIn('arith.select', result.stdout)

    def test_power_two_division_signed_extremes_execute(self):
        for divisor in (2, 8, 1 << 62):
            for value in (-(1 << 63), (1 << 63)-1, -9, -8, -7, -1, 0, 1, 7, 8, 9):
                quotient = abs(value) // divisor * (-1 if value < 0 else 1)
                for operation, expected in [('divsi', quotient), ('remsi', value-quotient*divisor)]:
                    with self.subTest(divisor=divisor, value=value, operation=operation):
                        source = f"module {{ func.func @main() -> i64 {{ %x = arith.constant {value} : i64\n %d = arith.constant {divisor} : i64\n %r = arith.{operation} %x, %d : i64\n return %r : i64 }} }}"
                        lowered = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
                        self.assertEqual(lowered.returncode, 0, lowered.stderr)
                        self.assertEqual(self.execute(lowered.stdout), expected)

    def test_non_power_two_division_is_preserved(self):
        source = "module { func.func @f(%x: i64) -> i64 { %d = arith.constant 3 : i64\n %r = arith.divsi %x, %d : i64\n return %r : i64 } }"
        result = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.divsi', result.stdout)

    def test_power_two_index_division_rewrites_without_changing_type(self):
        source = "module { func.func @f(%x: index) -> (index, index) { %d = arith.constant 4 : index\n %q = arith.divsi %x, %d : index\n %r = arith.remsi %x, %d : index\n return %q, %r : index, index } }"
        result = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('arith.divsi', result.stdout)
        self.assertNotIn('arith.remsi', result.stdout)
        self.assertIn('arith.shrsi', result.stdout)
        self.assertNotIn('arith.index_cast', result.stdout)

    def test_power_two_index_division_executes_at_both_index_widths(self):
        tool = os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        runner = os.environ.get('LLM2FPGA_MLIR_RUNNER')
        self.assertTrue(tool and runner, 'pinned driver and runner required')
        for width in (32, 64):
            for divisor in (4, 1 << 30):
                for value in (-(1 << (width-1)), (1 << (width-1))-1, -9, -1, 0, 9):
                    quotient = abs(value) // divisor * (-1 if value < 0 else 1)
                    for operation, expected in [('divsi', quotient), ('remsi', value-quotient*divisor)]:
                        with self.subTest(width=width, divisor=divisor, value=value, operation=operation):
                            # Keep the operation in a separate function with an
                            # argument: the test must execute the rewritten body.
                            source = f'''module {{
func.func @f(%x: index) -> index {{
 %d = arith.constant {divisor} : index
 %r = arith.{operation} %x, %d : index
 return %r : index
}}
func.func @main() -> i64 {{
 %x = arith.constant {value} : index
 %r = func.call @f(%x) : (index) -> index
 %v = arith.index_cast %r : index to i64
 return %v : i64
}} }}'''
                            lowered = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
                            self.assertEqual(lowered.returncode, 0, lowered.stderr)
                            self.assertNotIn('arith.' + operation, lowered.stdout)
                            with tempfile.TemporaryDirectory() as directory:
                                path = Path(directory) / 'llvm.mlir'
                                converted = subprocess.run([tool,
                                    f'--convert-arith-to-llvm=index-bitwidth={width}',
                                    f'--convert-func-to-llvm=index-bitwidth={width}',
                                    '--reconcile-unrealized-casts', '-o', str(path)],
                                    input=lowered.stdout, text=True, capture_output=True)
                                self.assertEqual(converted.returncode, 0, converted.stderr)
                                executed = subprocess.run([runner, str(path), '-e', 'main', '--entry-point-result=i64'], text=True, capture_output=True)
                                self.assertEqual(executed.returncode, 0, executed.stderr)
                                self.assertEqual(int(executed.stdout.strip()), expected)

    def test_index_divisor_outside_shared_positive_domain_is_preserved(self):
        source = 'module { func.func @f(%x: index) -> index { %d = arith.constant 2147483648 : index\n %r = arith.divsi %x, %d : index\n return %r : index } }'
        result = self.run_pass(source, 'llm2fpga-strength-reduce-integer-constants')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.divsi', result.stdout)
