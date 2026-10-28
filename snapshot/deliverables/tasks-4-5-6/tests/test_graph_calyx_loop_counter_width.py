"""Narrow only statically bounded nonoverflowing induction state."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


def fixture(lower=0, upper=64, step=1, counter_type='index'):
    cast = ('%v = arith.index_cast %j : index to i64' if counter_type == 'index'
            else '%v = arith.extsi %j : i32 to i64' if counter_type == 'i32'
            else '%v = arith.addi %j, %zero : i64')
    return f'''module {{
      func.func @main() -> i64 {{
        %lb = arith.constant {lower} : {counter_type}
        %ub = arith.constant {upper} : {counter_type}
        %step = arith.constant {step} : {counter_type}
        %zero = arith.constant 0 : i64
        %loop:2 = scf.while (%i = %lb, %a = %zero) : ({counter_type}, i64) -> ({counter_type}, i64) {{
          %c = arith.cmpi slt, %i, %ub : {counter_type}
          scf.condition(%c) %i, %a : {counter_type}, i64
        }} do {{
        ^bb0(%j: {counter_type}, %sum: i64):
          {cast}
          %updated = arith.addi %sum, %v : i64
          %next = arith.addi %j, %step : {counter_type}
          scf.yield %next, %updated : {counter_type}, i64
        }}
        return %loop#1 : i64
      }}
    }}'''


class LoopCounterWidthTests(unittest.TestCase):
    def transform(self, source):
        result = subprocess.run([os.environ['LLM2FPGA_MEMORY_TOOL'],
            '--verify-each', '--llm2fpga-narrow-static-loop-counters'],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def execute(self, source):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'llvm.mlir'
            result = subprocess.run([os.environ['LLM2FPGA_FLATTEN_MLIR_TOOL'],
                '--convert-scf-to-cf', '--convert-arith-to-llvm',
                '--convert-cf-to-llvm', '--convert-func-to-llvm',
                '--reconcile-unrealized-casts', '-o', str(output)],
                input=source, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([os.environ['LLM2FPGA_FLATTEN_MLIR_RUNNER'],
                str(output), '-e', 'main', '--entry-point-result=i64'],
                text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            return int(result.stdout.strip())

    def test_counter_carries_positive_exit_value_and_preserves_other_state(self):
        for lower, upper, step, width in ((0, 1, 1, 2), (0, 64, 1, 8),
                                         (257, 333, 3, 10), (0, 9, 3, 5)):
            with self.subTest(lower=lower, upper=upper, step=step):
                source = fixture(lower, upper, step)
                transformed = self.transform(source)
                self.assertIn(f'-> (i{width}, i64)', transformed)
                self.assertEqual(self.transform(transformed), transformed)
                expected = sum(range(lower, upper, step))
                self.assertEqual(self.execute(source), expected)
                self.assertEqual(self.execute(transformed), expected)

    def test_empty_negative_or_overflowing_domains_are_not_narrowed(self):
        for lower, upper, step in ((4, 4, 1), (-1, 8, 1), (0, 8, -1),
                                   (0, 2147483647, 2)):
            with self.subTest(lower=lower, upper=upper, step=step):
                result = self.transform(fixture(lower, upper, step))
                self.assertIn('-> (index, i64)', result)

    def test_dynamic_bound_is_not_narrowed(self):
        source = fixture().replace('func.func @main()', 'func.func @main(%bound: index)')
        source = source.replace('%i, %ub : index', '%i, %bound : index')
        self.assertIn('-> (index, i64)', self.transform(source))

    def test_integer_counter_types_preserve_signed_widening(self):
        for counter_type in ('i32', 'i64'):
            with self.subTest(counter_type=counter_type):
                source = fixture(counter_type=counter_type)
                transformed = self.transform(source)
                self.assertIn('-> (i8, i64)', transformed)
                self.assertEqual(self.execute(source), 2016)
                self.assertEqual(self.execute(transformed), 2016)

    def test_noncanonical_progress_is_not_narrowed(self):
        source = fixture().replace('arith.addi %j, %step', 'arith.addi %j, %j')
        self.assertIn('-> (index, i64)', self.transform(source))

    def test_exit_counter_retains_original_public_index_type(self):
        source = fixture().replace('return %loop#1 : i64',
            '%exit = arith.index_cast %loop#0 : index to i64\nreturn %exit : i64')
        self.assertEqual(self.execute(source), 64)
        self.assertEqual(self.execute(self.transform(source)), 64)

    def test_native_calyx_and_rtl_execute_the_narrowed_loop(self):
        from compiler.pipeline import CALYX_RESOURCE_PASSES
        calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        for source in (fixture(), self.transform(fixture())):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lowered = subprocess.run([os.environ['LLM2FPGA_MEMORY_TOOL'],
                    '--verify-each', '--lower-scf-to-calyx=top-level-function=main'],
                    input=source, text=True, capture_output=True, timeout=30)
                self.assertEqual(lowered.returncode, 0, lowered.stderr)
                emitted = subprocess.run([os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL'],
                    '--export-calyx'], input=lowered.stdout, text=True,
                    capture_output=True, timeout=30)
                self.assertEqual(emitted.returncode, 0, emitted.stderr)
                (root/'source.futil').write_text(emitted.stdout)
                result = subprocess.run([str(calyx), 'source.futil', '-b', 'verilog',
                    '--synthesis', '--nested', *CALYX_RESOURCE_PASSES, '-l',
                    str(calyx.parent.parent/'share/calyx'), '-o', 'kernel.sv'],
                    cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                # The expected value is supplied to the checker after emission.
                (root/'tb.sv').write_text('''module tb;
reg clk=0,reset=1,go=0;wire done;wire[63:0]out0;integer cycles=0;
main dut(.*);always #5 clk=~clk;
initial begin repeat(3)@(negedge clk);reset=0;go=1;
while(!done&&cycles<5000)begin @(negedge clk);cycles=cycles+1;end
if(!done||out0!==64'd2016)$fatal(1,"loop result/termination");
$display("PASS loop sum=2016");$finish;end endmodule''')
                result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s',
                    'tb', '-o', 'sim', 'tb.sv', 'kernel.sv'], cwd=root,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([shutil.which('vvp'), 'sim'], cwd=root,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn('PASS loop sum=2016', result.stdout)


if __name__ == '__main__':
    unittest.main()
