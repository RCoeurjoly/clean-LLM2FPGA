"""Qualify the pinned primitive's completed-transaction reuse, without resets."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


class SignedDividerProtocolTests(unittest.TestCase):
    def test_completed_divider_reuses_state_and_holds_results_while_idle(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        primitive = tool.parent.parent/'share/calyx/primitives/binary_operators.sv'
        compiler, runtime = shutil.which('iverilog'), shutil.which('vvp')
        self.assertIsNotNone(compiler)
        self.assertIsNotNone(runtime)
        for width in (8, 16, 64):
            mask = (1 << width)-1
            edge = 1 << (width-1)
            values = list(range(256)) if width == 8 else [0,1,-1,7,-7,edge-1,-edge]
            signed = lambda value: (value & mask)-(1 << width) if value & edge else value & mask
            cases = [(signed(value), denominator) for denominator in
                     (1,-1,3,-5,edge-1,-edge) for value in values]
            initializers = '\n'.join(
                f'lefts[{index}]={width}\'h{left & mask:x}; '
                f'rights[{index}]={width}\'h{right & mask:x};'
                for index,(left,right) in enumerate(cases))
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root/'tb.sv').write_text(f'''module tb;
localparam WIDTH={width}, CASES={len(cases)};
reg clk=0,reset=1,go=0;
reg signed[WIDTH-1:0] left=0,right=1;
wire signed[WIDTH-1:0] out_quotient,out_remainder;wire done;
reg[WIDTH-1:0] lefts[0:CASES-1],rights[0:CASES-1],saved_q,saved_r;
integer index,cycles;
std_sdiv_pipe #(.WIDTH(WIDTH)) dut(.*);
always #5 clk=~clk;
initial begin
{initializers}
 repeat(3)@(negedge clk);reset=0;
 for(index=0;index<CASES;index=index+1)begin
  go=0;cycles=0;
  while(done && cycles<4)begin @(negedge clk);cycles=cycles+1;end
  if(done)$fatal(1,"completion did not retire");
  left=lefts[index];right=rights[index];go=1;cycles=0;
  while(!done && cycles<WIDTH+8)begin @(negedge clk);cycles=cycles+1;end
  if(!done)$fatal(1,"transaction did not complete");
  saved_q=out_quotient;saved_r=out_remainder;
  $display("RESULT %0d %h %h",index,saved_q,saved_r);
  go=0;left=~left;right=~right;
  repeat(3)begin
   @(negedge clk);
   if(out_quotient!==saved_q||out_remainder!==saved_r)
    $fatal(1,"idle operand change altered completed result");
  end
 end
 $finish;
end
endmodule
''')
                built = subprocess.run([compiler,'-g2012','-s','tb','-o','simulator',
                    'tb.sv',str(primitive)],cwd=root,capture_output=True,text=True,timeout=30)
                self.assertEqual(built.returncode,0,built.stderr)
                run = subprocess.run([runtime,'simulator'],cwd=root,capture_output=True,
                                     text=True,timeout=60)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                rows = re.findall(r'^RESULT (\d+) ([0-9a-f]+) ([0-9a-f]+)$',
                                  run.stdout,re.MULTILINE)
                self.assertEqual(len(rows),len(cases))
                for index,((left,right),row) in enumerate(zip(cases,rows)):
                    quotient = abs(left)//abs(right)
                    if (left<0)!=(right<0):quotient=-quotient
                    self.assertEqual((int(row[0]),int(row[1],16),int(row[2],16)),
                        (index,quotient & mask,(left % right) & mask))
