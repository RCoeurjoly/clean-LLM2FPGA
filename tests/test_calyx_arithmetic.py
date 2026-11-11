import os
import json
import math
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest


class ArithmeticTests(unittest.TestCase):
    def run_tool(self, *command, source=None):
        result = subprocess.run(command, input=source, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def check(self, source, pairs, input_width, output_width, ulps=0, synthesize=False):
        widths = input_width if isinstance(input_width, tuple) else (input_width,)
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            os.chdir(temporary)
            try:
                source = self.run_tool(os.environ['CALYX_OPT'],
                    '--prepare-calyx-math', '--prepare-calyx-arithmetic', source=source)
                source = self.run_tool(os.environ['MLIR_OPT'], '--arith-expand', source=source)
                lowered = self.run_tool(os.environ['CALYX_OPT'],
                    '--calyx-float-cast-source=' + os.environ['FLOAT_CAST'],
                    '--lower-scf-to-calyx=top-level-function=main write-json=memory',
                    '--symbol-dce',
                    source=source)
                program = self.run_tool(os.environ['CALYX_TRANSLATE'],
                    '--export-calyx', source=lowered)
                Path('main.futil').write_text(program)
                self.run_tool(os.environ['CALYX'], 'main.futil', '-b', 'verilog',
                              '-l', os.environ['CALYX_LIBRARY'],
                              '--synthesis', '-x', 'cell-share:bounds=1,-1,-1',
                              '-o', 'main.sv')
                rtl = 'main.sv'
                if synthesize:
                    self.run_tool(os.environ['YOSYS'], '-Q', '-T', '-p',
                        'read_verilog -sv main.sv; hierarchy -check -top main; proc; '
                        'flatten; opt_clean; check -assert; write_verilog -noattr lowered.sv')
                    rtl = 'lowered.sv'
                initialize = []
                if Path('memory.json').exists():
                    for name, memory in json.loads(Path('memory.json').read_text()).items():
                        width = memory['format']['width']
                        for index, value in enumerate(memory['data']):
                            bits = int.from_bytes(struct.pack('<f', value), 'little')
                            initialize.append(f"dut.{name}.mem[{index}]={width}'h{bits:x};")
                calls = []
                for value, expected in pairs:
                    limit = ulps
                    if output_width == 32 and expected & 0x7f800000 == 0x7f800000:
                        limit = 0
                    values = value if isinstance(value, tuple) else (value,)
                    assign = ''.join(f"in{i}={width}'h{v:x};"
                                     for i, (v, width) in enumerate(zip(values, widths)))
                    calls.append(f'''{assign}go=1;cycles=0;
                      while(done!==1 && cycles<1000) begin @(negedge clk);cycles=cycles+1;end
                      if(done!==1 || (^out0===1'bx) || (out0>{output_width}'h{expected:x} ?
                        out0-{output_width}'h{expected:x} :
                        {output_width}'h{expected:x}-out0)>{limit})
                        $fatal(1,"input=%h output=%h expected={expected:x}",in0,out0);
                      go=0;repeat(4)@(negedge clk);''')
                Path('tb.sv').write_text(f'''module tb;
                  reg clk=0,reset=1,go=0;
                  {''.join(f'reg[{w-1}:0]in{i}=0;' for i,w in enumerate(widths))}
                  wire done;wire[{output_width-1}:0]out0;integer cycles;
                  main dut(.*);always #5 clk=~clk;
                  initial begin {''.join(initialize)}repeat(3)@(negedge clk);reset=0;
                    {''.join(calls)}
                    $display("PASS arithmetic {len(pairs)}");$finish;end endmodule''')
                self.run_tool(os.environ['IVERILOG'], '-g2012', '-s', 'tb', '-o', 'sim',
                              'tb.sv', rtl)
                print(self.run_tool(os.environ['VVP'], 'sim').strip())
            finally:
                os.chdir(previous)

    def test_model_boolean_to_float(self):
        source = (Path(__file__).parent / 'cases/boolean-to-float.mlir').read_text()
        self.check(source, [(0, 0), (1, 0x3f800000), (0, 0), (1, 0x3f800000)], 1, 32)

    def test_model_minimum_and_maximum(self):
        pairs = [(0, 0x80000000), (0x80000000, 0), (0x3f800000, 0xbf800000),
                 (0xbf800000, 0x3f800000), (0x7f800000, 0x3f800000),
                 (0x7fc00000, 0x3f800000), (0x3f800000, 0x7fc00000)]
        minimum = [0x80000000, 0x80000000, 0xbf800000, 0xbf800000,
                   0x3f800000, 0x7fc00000, 0x7fc00000]
        maximum = [0, 0, 0x3f800000, 0x3f800000, 0x7f800000, 0x7fc00000, 0x7fc00000]
        for name, expected in [('minimum', minimum), ('maximum', maximum)]:
            source = (Path(__file__).parent / f'cases/{name}.mlir').read_text()
            with self.subTest(name=name):
                self.check(source, list(zip(pairs, expected)), (32, 32), 32)

    def test_model_float_format_conversions(self):
        values = [0., -0., 1., -1., 1.0000000596046448, 1.0000001788139343,
                  1e-40, 1e-30, 3.4028234663852886e38, float('inf'), -float('inf')]
        for name, source_format, target_format in [('extend', 'f', 'd'), ('truncate', 'd', 'f')]:
            pairs = []
            for value in values:
                encoded = struct.pack('<' + source_format, value)
                rounded = struct.unpack('<' + source_format, encoded)[0]
                expected = struct.pack('<' + target_format, rounded)
                pairs.append((int.from_bytes(encoded, 'little'),
                              int.from_bytes(expected, 'little')))
            source = (Path(__file__).parent / f'cases/{name}-float.mlir').read_text()
            with self.subTest(name=name):
                self.check(source, pairs, len(encoded) * 8, len(expected) * 8)

    def test_model_resource_weight_reaches_rtl(self):
        source = (Path(__file__).parent / 'cases/resource-weight.mlir').read_text()
        self.check(source, [(0, 0x3f8cd1ec)], 1, 32)

    def test_model_memory_copy_preserves_checkpoint_value(self):
        source = (Path(__file__).parent / 'cases/resource-copy.mlir').read_text()
        self.check(source, [(0, 0x3f8cd1ec)], 1, 32)

    def test_model_rounding_to_nearest_even(self):
        values = [0., -0., .1, -.1, .5, -.5, 1.5, -1.5, 2.5, -2.5, 3.7, -3.7,
                  1e20, -1e20, float('inf'), -float('inf')]
        for width, fmt in [(32, 'f'), (64, 'd')]:
            pairs = []
            for value in values:
                encoded = struct.pack('<' + fmt, value)
                value = struct.unpack('<' + fmt, encoded)[0]
                result = (math.copysign(float(round(value)), value)
                          if math.isfinite(value) else value)
                expected = struct.pack('<' + fmt, result)
                pairs.append((int.from_bytes(encoded, 'little'),
                              int.from_bytes(expected, 'little')))
            source = (Path(__file__).parent / f'cases/round{width}.mlir').read_text()
            with self.subTest(width=width):
                self.check(source, pairs, width, width)

    def test_model_reciprocal_sqrt(self):
        pairs = []
        for value in [0., 1., 4., .25, 1e-10, 1e10, float('inf')]:
            encoded = struct.pack('<f', value)
            value = struct.unpack('<f', encoded)[0]
            expected = 1 / math.sqrt(value) if value else float('inf')
            pairs.append((int.from_bytes(encoded, 'little'),
                          int.from_bytes(struct.pack('<f', expected), 'little')))
        source = (Path(__file__).parent / 'cases/reciprocal-sqrt.mlir').read_text()
        self.check(source, pairs, 32, 32, ulps=2, synthesize=True)

    def test_model_exp_and_tanh(self):
        values = [-80., -20., -10., -2., -.1, -1e-8, -0., 0., 1e-8, .1, 2., 10., 20., 80.]
        for name, function in [('exp', math.exp), ('tanh', math.tanh)]:
            pairs = []
            for value in values:
                encoded = struct.pack('<f', value)
                value = struct.unpack('<f', encoded)[0]
                expected = struct.pack('<f', function(value))
                pairs.append((int.from_bytes(encoded, 'little'),
                              int.from_bytes(expected, 'little')))
            if name == 'exp':
                pairs += [(0xc2c80000, 0), (0xff800000, 0), (0x7f800000, 0x7f800000)]
            source = (Path(__file__).parent / f'cases/{name}.mlir').read_text()
            with self.subTest(name=name):
                self.check(source, pairs, 32, 32, ulps=4)

    def test_polynomial_fma_is_fused(self):
        source = (Path(__file__).parent / 'cases/fma.mlir').read_text()
        a, b, c = 1 + 2**-23, 1 - 2**-23, -1.
        bits = lambda x: int.from_bytes(struct.pack('<f', x), 'little')
        pairs = [((bits(a), bits(b), bits(c)), bits(-2**-46)),
                 ((bits(2.), bits(3.), bits(-1.)), bits(5.)),
                 ((bits(0.), bits(2.), bits(-0.)), bits(0.))]
        self.check(source, pairs, (32, 32, 32), 32)

    def test_backend_rejects_directed_float_rounding(self):
        source = (Path(__file__).parent / 'cases/truncate-float.mlir').read_text()
        source = source.replace('%arg0 : f64 to f32', '%arg0 upward : f64 to f32')
        result = subprocess.run([os.environ['CALYX_OPT'],
            '--lower-scf-to-calyx=top-level-function=main'], input=source,
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsupported floating-point rounding mode', result.stderr)

    def test_unsupported_math_is_rejected_with_memory_export(self):
        source = (Path(__file__).parent / 'cases/round64.mlir').read_text()
        source = source.replace('math.roundeven', 'math.round')
        result = subprocess.run([os.environ['CALYX_OPT'],
            '--lower-scf-to-calyx=top-level-function=main write-json=memory'],
            input=source, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
