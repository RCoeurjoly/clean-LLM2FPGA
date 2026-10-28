"""Unsigned index casts preserve bits at the pinned Calyx 32-bit index boundary."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UnsignedIndexCastTests(unittest.TestCase):
    def test_unsupported_memref_cast_is_diagnosed_without_aborting(self):
        source = '''module {
          func.func @main(%m: memref<4xi16>) -> i64 {
            %cast = arith.index_castui %m : memref<4xi16> to memref<4xindex>
            %zero = arith.constant 0 : index
            %v = memref.load %cast[%zero] : memref<4xindex>
            %r = arith.index_castui %v : index to i64
            return %r : i64
          }
        }'''
        result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--lower-scf-to-calyx=top-level-function=main'],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("failed to legalize operation 'arith.index_castui'", result.stderr)

    def check_cast(self, source_type, intermediate_type, input_width, mask_width):
        # Keep inputs as runtime ports so constants cannot hide a missing cast.
        source = f'''module {{
          func.func @main(%value: {source_type}) -> i64 {{
            %middle = arith.index_castui %value : {source_type} to {intermediate_type}
            %result = {'arith.index_castui' if intermediate_type == 'index' else 'arith.extui'} %middle : {intermediate_type} to i64
            return %result : i64
          }}
        }}'''
        values = sorted({0, 1, (1 << (input_width - 1)) - 1,
                         1 << (input_width - 1), (1 << input_width) - 1,
                         0x8000 & ((1 << input_width) - 1),
                         0x123456788765FEDC & ((1 << input_width) - 1)})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lowered = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
                '--verify-each', '--lower-scf-to-calyx=top-level-function=main'],
                input=source, text=True, capture_output=True, timeout=30)
            self.assertEqual(lowered.returncode, 0, lowered.stderr)
            self.assertNotIn('arith.index_castui', lowered.stdout)
            emitted = subprocess.run([os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL'],
                '--export-calyx'], input=lowered.stdout, text=True,
                capture_output=True, timeout=30)
            self.assertEqual(emitted.returncode, 0, emitted.stderr)
            (root / 'source.futil').write_text(emitted.stdout)
            calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
            from compiler.pipeline import CALYX_RESOURCE_PASSES
            compiled = subprocess.run([str(calyx), 'source.futil', '-b', 'verilog',
                '--synthesis', '--nested', *CALYX_RESOURCE_PASSES,
                '-l', str(calyx.parent.parent / 'share/calyx'), '-o', 'kernel.sv'],
                cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checks = []
            for value in values:
                expected = value & ((1 << mask_width) - 1)
                checks.append(f'''reset=1;go=0;repeat(3)@(negedge clk);
                  in0={input_width}'h{value:x};reset=0;go=1;cycles=0;
                  while(!done&&cycles<100)begin @(negedge clk);cycles=cycles+1;end
                  if(!done||out0!==64'h{expected:x})
                    $fatal(1,"unsigned cast in=%h got=%h expected=%h",in0,out0,64'h{expected:x});
                  go=0;@(negedge clk);''')
            (root / 'tb.sv').write_text(f'''module tb;
              reg clk=0,reset=1,go=0;reg[{input_width - 1}:0]in0=0;
              wire done;wire[63:0]out0;integer cycles=0;
              main dut(.*);always #5 clk=~clk;
              initial begin {''.join(checks)}
                $display("PASS unsigned casts {len(values)}");$finish;
              end endmodule''')
            result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s',
                'tb', '-o', 'sim', 'tb.sv', 'kernel.sv'], cwd=root,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([shutil.which('vvp'), 'sim'], cwd=root,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f'PASS unsigned casts {len(values)}', result.stdout)

    def test_unsigned_integer_to_index_zero_extension_and_truncation(self):
        for width in (8, 16, 32, 64):
            with self.subTest(width=width):
                self.check_cast(f'i{width}', 'index', width, min(width, 32))

    def test_unsigned_index_to_integer_truncation_and_equal_width(self):
        for width in (8, 16, 32):
            with self.subTest(width=width):
                self.check_cast('index', f'i{width}', 32, width)


if __name__ == '__main__':
    unittest.main()
