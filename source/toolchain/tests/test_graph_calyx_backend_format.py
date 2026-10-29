"""Native backend formatting retains large guard-disjointness assertions."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil


class CalyxBackendFormattingTests(unittest.TestCase):
    def emit(self, directory, count, same_value=False, synthesis=False, nested=True):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        library = tool.parent.parent / 'share' / 'calyx'
        source = directory / 'input.futil'
        output = directory / 'output.sv'
        # Distinct values prevent the compiler merging all assignments into one
        # guard. This is authored compiler input, independent of model oracles.
        assignments = '\n'.join(
            f"chosen.in = selector == 16'd{i} ? 32'd{1 if same_value else i};"
            for i in range(count))
        source.write_text('import "primitives/core.futil";\n'
                          'component main(selector: 16) -> (result: 32) {\n'
                          'cells { chosen = std_wire(32); }\n'
                          'wires {\n' + assignments + '\n'
                          "result = chosen.out; done = 1'd1;\n}\ncontrol {}\n}\n")
        options = ['--synthesis'] if synthesis else []
        if synthesis and nested:options.append('--nested')
        subprocess.run([str(tool), str(source), '-b', 'verilog', '-l',
                        str(library), *options, '-o', str(output)], check=True,
                       capture_output=True, text=True, timeout=60)
        return output.read_text()


    def test_nested_guard_serialization_preserves_four_state_results(self):
        testbench="""module tb;
 reg[15:0]selector=0;wire[31:0]result;wire done;
 integer i,j;reg guard;reg[31:0]expected;
 main dut(.selector(selector),.result(result),.go(1'b0),.clk(1'b0),.reset(1'b0),.done(done));
 task check;
 begin
  #1;guard=0;
  for(j=0;j<512;j=j+1)guard=guard | (selector==16'(j));
  expected=guard ? 32'd1 : 32'd0;
  if(result!==expected)$fatal(1,"guard differs selector=%h result=%h expected=%h",selector,result,expected);
 end
 endtask
 initial begin
  for(i=0;i<514;i=i+1)begin selector=i;check();end
  selector=16'hffff;check();selector=16'hxxxx;check();selector=16'hzzzz;check();
  selector=16'b00000000000000x1;check();selector=16'b10000000000000x1;check();
  $display("PASS four-state known=515 unknown=4");$finish;
 end
endmodule
"""
        for nested in (True,False):
            with self.subTest(nested=nested),tempfile.TemporaryDirectory() as work:
                directory=Path(work)
                self.emit(directory,512,same_value=True,synthesis=True,nested=nested)
                (directory/'tb.sv').write_text(testbench)
                result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',
                    str(directory/'simulator'),str(directory/'output.sv'),str(directory/'tb.sv')],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result=subprocess.run([shutil.which('vvp'),str(directory/'simulator')],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('PASS four-state known=515 unknown=4',result.stdout)


if __name__ == '__main__':
    unittest.main()
