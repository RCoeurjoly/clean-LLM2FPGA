"""Native sequential signed division must use qualified resource sharing."""
import os
from pathlib import Path
import re
import random
import shutil
import subprocess
import tempfile
import unittest


class SignedDividerSharingTests(unittest.TestCase):
    def library(self, tool):
        return Path(os.environ.get('LLM2FPGA_CALYX_LIBRARY',
                                   tool.parent.parent / 'share/calyx'))

    def test_primitive_repeated_transactions_idle_retention_and_reset(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        iverilog = os.environ.get('LLM2FPGA_IVERILOG', shutil.which('iverilog'))
        self.assertIsNotNone(iverilog, 'Icarus is required')
        library = self.library(tool) / 'primitives/binary_operators.sv'
        for width in (8, 64):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                mask = (1 << width) - 1
                low, high = -(1 << (width - 1)), (1 << (width - 1)) - 1
                values = (low, low + 1, -17, -1, 0, 1, 17, high)
                cases = [(a, b) for a in values for b in values if b]
                rng = random.Random(1701)
                cases += [(rng.randint(low, high), rng.choice((-17, -3, -1, 1, 3, 17)))
                          for _ in range(128)]
                calls = []
                for left, right in cases:
                    quotient = abs(left) // abs(right)
                    if (left < 0) != (right < 0):
                        quotient = -quotient
                    calls.append(f"calculate({width}'h{left & mask:x}, "
                                 f"{width}'h{right & mask:x}, "
                                 f"{width}'h{quotient & mask:x}, "
                                 f"{width}'h{(left % right) & mask:x});")
                bench = '''module tb;
localparam WIDTH = @WIDTH@;
reg clk=0, reset=1, go=0;
reg [WIDTH-1:0] left=0, right=1;
wire [WIDTH-1:0] out_quotient, out_remainder;
wire done;
std_sdiv_pipe #(.WIDTH(WIDTH)) dut(.*);
always #5 clk=~clk;
task calculate(input [WIDTH-1:0] a,b,q,r);
integer cycles;
begin
  @(negedge clk); left=a; right=b; go=1; cycles=0;
  while (!done && cycles<WIDTH+5) begin
    @(negedge clk); cycles=cycles+1;
  end
  if (!done) $fatal(1,"division did not complete");
  if (out_quotient !== q || out_remainder !== r)
    $fatal(1,"division mismatch a=%h b=%h q=%h r=%h expected=%h/%h",a,b,out_quotient,out_remainder,q,r);
  go=0;
  @(negedge clk); left=~a; right=~b;
  repeat(3) begin
    @(negedge clk);
    if(out_quotient !== q || out_remainder !== r)
      $fatal(1,"idle inputs changed retained outputs");
  end
end
endtask
initial begin
  repeat(2) @(negedge clk); reset=0;
  @CALLS@
  @(negedge clk); left=17; right=3; go=1;
  @(negedge clk); reset=1; go=0;
  repeat(2) @(negedge clk); reset=0;
  repeat(2) @(negedge clk);
  calculate(17,3,5,2);
  $display("LIFECYCLE_PASS"); $finish;
end
endmodule
'''.replace('@WIDTH@', str(width)).replace('@CALLS@', '\n'.join(calls))
                (root / 'tb.sv').write_text(bench)
                subprocess.run([iverilog, '-g2012', '-s', 'tb', '-o', 'sim',
                                str(root / 'tb.sv'), str(library)], cwd=root,
                               check=True, capture_output=True, text=True, timeout=60)
                result = subprocess.run([str(Path(iverilog).with_name('vvp')), 'sim'],
                                        cwd=root, check=True, capture_output=True,
                                        text=True, timeout=60)
                self.assertIn('LIFECYCLE_PASS', result.stdout)

    def test_exclusive_signed_divisions_share_one_primitive(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',
                                      Path(__file__).resolve().parents[1]))
        source = support / 'compiler/tests/calyx/signed-divider-sharing.futil'
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / 'shared.futil'
            subprocess.run([str(tool), str(source), '-b', 'calyx',
                            '-x', 'cell-share:bounds=1,-1,-1',
                            '-l', str(self.library(tool)),
                            '-o', str(output)], check=True,
                           capture_output=True, text=True, timeout=60)
            count = len(re.findall(r'=\s*std_sdiv_pipe\(8\)', output.read_text()))
        self.assertEqual(count, 1, 'exclusive signed divisions remain duplicated')

    def test_parallel_signed_divisions_remain_separate(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',
                                      Path(__file__).resolve().parents[1]))
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / 'parallel.futil'
            subprocess.run([str(tool), str(support / 'compiler/tests/calyx/signed-divider-parallel.futil'),
                            '-b', 'calyx', '-x', 'cell-share:bounds=1,-1,-1',
                            '-l', str(self.library(tool)), '-o', str(output)],
                           check=True, capture_output=True, text=True, timeout=60)
            self.assertEqual(len(re.findall(r'=\s*std_sdiv_pipe\(8\)', output.read_text())), 2)

    def test_checked_callers_preserve_both_signed_results(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        iverilog = os.environ.get('LLM2FPGA_IVERILOG', shutil.which('iverilog'))
        self.assertIsNotNone(iverilog, 'Icarus is required')
        support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',
                                      Path(__file__).resolve().parents[1]))
        rng = random.Random(1701)
        calls = []
        for _ in range(128):
            a, c = rng.randint(-128, 127), rng.randint(-128, 127)
            b, d = rng.choice((-17, -3, -1, 1, 3, 17)), rng.choice((-17, -3, -1, 1, 3, 17))
            def quotient(x, y):
                q = abs(x) // abs(y)
                return (-q if (x < 0) != (y < 0) else q) & 255
            calls.append('calculate(' + ','.join(f"8'h{x & 255:x}" for x in
                         (a, b, c, d, quotient(a, b), quotient(c, d))) + ');')
        for name in ('signed-divider-sharing', 'signed-divider-parallel'):
            with self.subTest(caller=name), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                subprocess.run([str(tool), str(support / f'compiler/tests/calyx/{name}.futil'),
                                '-b', 'verilog', '-x', 'cell-share:bounds=1,-1,-1',
                                '-x', 'tdcc:infer-fsms=true',
                                '-l', str(self.library(tool)), '-o', str(root / 'kernel.sv')],
                               check=True, capture_output=True, text=True, timeout=60)
                # Unshared sequential ports have one driver, so the backend
                # correctly emits no disjoint-assignment check for them.
                (root / 'tb.sv').write_text('''module tb;
reg clk=0, reset=1, go=0;
reg [7:0] left0=0,right0=1,left1=0,right1=1;
wire [7:0] first_result,second_result;
wire done;
main dut(.*);
always #5 clk=~clk;
task calculate(input [7:0] a,b,c,d,q0,q1);
integer cycles;
begin
 @(negedge clk); left0=a; right0=b; left1=c; right1=d; go=1; cycles=0;
 while (!done && cycles<80) begin @(negedge clk); cycles=cycles+1; end
 if (!done) $fatal(1,"caller did not finish");
 if(first_result !== q0 || second_result !== q1) $fatal(1,"caller quotient mismatch");
 go=0; repeat(3) @(negedge clk);
end
endtask
initial begin
 repeat(2) @(negedge clk); reset=0;
 @CALLS@
 $display("CALLER_PASS"); $finish;
end
endmodule
'''.replace('@CALLS@', '\n'.join(calls)))
                subprocess.run([iverilog, '-g2012', '-s', 'tb', '-o', 'sim',
                                'tb.sv', 'kernel.sv'], cwd=root, check=True,
                               capture_output=True, text=True, timeout=60)
                result = subprocess.run([str(Path(iverilog).with_name('vvp')), 'sim'],
                                        cwd=root, check=False, capture_output=True,
                                        text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('CALLER_PASS', result.stdout)
                self.assertNotRegex(result.stdout + result.stderr, r'(?i)\b(error|fatal)\b')
