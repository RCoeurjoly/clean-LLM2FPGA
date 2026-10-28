"""Exclusive control does not justify cyclic combinational resource sharing."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from compiler.pipeline import integer_profile


class CalyxSharingTests(unittest.TestCase):
    def emit(self, root):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',
                                      Path(__file__).resolve().parents[1]))
        source = support / 'compiler/tests/calyx/combinational-sharing-cycle.futil'
        output = root / 'kernel.sv'
        arguments = integer_profile()[-1][2]
        subprocess.run([str(tool), str(source), *arguments, '--synthesis', '--nested',
                        '-l', str(tool.parent.parent / 'share/calyx'), '-o', str(output)],
                       check=True, capture_output=True, text=True, timeout=60)
        return output

    def test_opposite_operator_orders_have_no_combinational_cycle(self):
        yosys = shutil.which('yosys')
        self.assertIsNotNone(yosys, 'installed Yosys required')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            self.emit(root)
            result = subprocess.run([yosys, '-p',
                'read_verilog -sv kernel.sv; hierarchy -top main; proc; flatten; opt_clean; scc'],
                cwd=root, check=True, capture_output=True, text=True, timeout=60)
        self.assertTrue('Found 0 SCCs.' in result.stdout,
                        'Calyx sharing introduced a combinational cycle')

    def test_both_operator_orders_match_every_runtime_byte(self):
        iverilog, vvp = shutil.which('iverilog'), shutil.which('vvp')
        self.assertIsNotNone(iverilog, 'installed Icarus required')
        self.assertIsNotNone(vvp, 'installed Icarus runtime required')
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            self.emit(root)
            (root / 'tb.sv').write_text('''module tb;
reg clk=0, reset=1, go=0;
reg [7:0] value=0;
wire done;
wire [7:0] first_result, second_result;
main dut(.*);
always #5 clk=~clk;
integer index, cycles;
initial begin
  for(index=0; index<256; index=index+1) begin
    reset=1; go=0;
    repeat(2) @(negedge clk);
    reset=0; value=index; go=1; cycles=0;
    while(!done && cycles<80) begin
      @(negedge clk); cycles=cycles+1;
    end
    if(!done) $fatal(1, "generation did not complete");
    $display("RESULT %0d %0d %0d", index, first_result, second_result);
    go=0;
  end
  $finish;
end
endmodule
''')
            subprocess.run([iverilog, '-g2012', '-s', 'tb', '-o', 'simulator',
                            'tb.sv', 'kernel.sv'], cwd=root, check=True,
                           capture_output=True, text=True, timeout=60)
            result = subprocess.run([vvp, 'simulator'], cwd=root, check=True,
                                    capture_output=True, text=True, timeout=60)
        rows = re.findall(r'^RESULT (\d+) (\d+) (\d+)$', result.stdout, re.MULTILINE)
        self.assertEqual(len(rows), 256)
        for index, row in enumerate(rows):
            self.assertEqual(tuple(map(int, row)),
                             (index, ((index-1) & 255) ^ 3, ((index ^ 3)-1) & 255))
