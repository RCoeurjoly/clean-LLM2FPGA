"""Divider reuse requires exclusive activation and in-group result capture."""
import os
import subprocess
import unittest
import shutil
import tempfile
import re
from pathlib import Path

SOURCE = '''module attributes {calyx.entrypoint = "main"} {
 calyx.component @main(%value: i8, %divisor1: i8, %divisor2: i8,
 %clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) ->
 (%result1: i8, %result2: i8, %done: i1 {done}) {
  %true = hw.constant true
  %a.clk, %a.reset, %a.go, %a.left, %a.right, %a.out, %a.done =
   calyx.std_divs_pipe @a : i1, i1, i1, i8, i8, i8, i1
  %b.clk, %b.reset, %b.go, %b.left, %b.right, %b.out, %b.done =
   calyx.std_divs_pipe @b : i1, i1, i1, i8, i8, i8, i1
  %r1.in, %r1.we, %r1.clk, %r1.reset, %r1.out, %r1.done =
   calyx.register @r1 : i8, i1, i1, i1, i8, i1
  %r2.in, %r2.we, %r2.clk, %r2.reset, %r2.out, %r2.done =
   calyx.register @r2 : i8, i1, i1, i1, i8, i1
  calyx.wires {
   calyx.assign %result1 = %r1.out : i8
   calyx.assign %result2 = %r2.out : i8
   calyx.group @one {
    calyx.assign %a.left = %value : i8
    calyx.assign %a.right = %divisor1 : i8
    %not = comb.xor %a.done, %true : i1
    calyx.assign %a.go = %not ? %true : i1
    calyx.assign %r1.in = %a.out : i8
    calyx.assign %r1.we = %a.done : i1
    calyx.group_done %r1.done : i1
   }
   calyx.group @two {
    calyx.assign %b.left = %value : i8
    calyx.assign %b.right = %divisor2 : i8
    %not = comb.xor %b.done, %true : i1
    calyx.assign %b.go = %not ? %true : i1
    calyx.assign %r2.in = %b.out : i8
    calyx.assign %r2.we = %b.done : i1
    calyx.group_done %r2.done : i1
   }
  }
  calyx.control { calyx.seq { calyx.enable @one calyx.enable @two } }
 } {toplevel}
}
'''


class TransactionalDividerTests(unittest.TestCase):
    def transform(self, source=SOURCE):
        result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each','--llm2fpga-share-transactional-dividers'],
            input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout


    def test_native_shared_rtl_matches_original_across_repeated_transactions(self):
        from compiler.pipeline import CALYX_RESOURCE_PASSES
        translator = os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL']
        calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        compiler,runtime = shutil.which('iverilog'),shutil.which('vvp')
        self.assertIsNotNone(compiler);self.assertIsNotNone(runtime)
        outputs = []
        for source in (SOURCE,self.transform()):
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                emitted=subprocess.run([translator,'--export-calyx'],input=source,
                    text=True,capture_output=True,timeout=30)
                self.assertEqual(emitted.returncode,0,emitted.stderr)
                (root/'source.futil').write_text(emitted.stdout)
                subprocess.run([str(calyx),'source.futil','-b','verilog','--synthesis',
                    '--nested',*CALYX_RESOURCE_PASSES,'-l',str(calyx.parent.parent/'share/calyx'),
                    '-o','kernel.sv'],cwd=root,check=True,capture_output=True,text=True,timeout=60)
                (root/'tb.sv').write_text('''module tb;
reg clk=0,reset=1,go=0;reg[7:0]value=0,divisor1=3,divisor2=-5;
wire[7:0]result1,result2;wire done;integer pair,index,cycles;
main dut(.*);always #5 clk=~clk;
initial begin
 repeat(3)@(negedge clk);reset=0;
 for(pair=0;pair<5;pair=pair+1)begin
  case(pair)
   0:begin divisor1=3;divisor2=-5;end
   1:begin divisor1=-5;divisor2=3;end
   2:begin divisor1=-1;divisor2=1;end
   3:begin divisor1=127;divisor2=-128;end
   4:begin divisor1=-128;divisor2=127;end
  endcase
  for(index=0;index<256;index=index+1)begin
   go=0;value=255-index;repeat(3)@(negedge clk);
   value=index;go=1;cycles=0;
   while(!done&&cycles<120)begin @(negedge clk);cycles=cycles+1;end
   if(!done)$fatal(1,"shared transaction did not finish");
   $display("RESULT %0d %0d %0d %0d",pair,index,result1,result2);
   go=0;repeat(3)@(negedge clk);
  end
 end
 $finish;
end
endmodule
''')
                subprocess.run([compiler,'-g2012','-s','tb','-o','simulator','tb.sv','kernel.sv'],
                    cwd=root,check=True,capture_output=True,text=True,timeout=30)
                result=subprocess.run([runtime,'simulator'],cwd=root,capture_output=True,
                    text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                outputs.append(re.findall(r'^RESULT (\d+) (\d+) (\d+) (\d+)$',
                                           result.stdout,re.MULTILINE))
        self.assertEqual(outputs[0],outputs[1])
        self.assertEqual(len(outputs[1]),1280)
        pairs=[(3,-5),(-5,3),(-1,1),(127,-128),(-128,127)]
        for row in outputs[1]:
            pair,index,a,b=map(int,row);value=index if index<128 else index-256
            expected=[]
            for denominator in pairs[pair]:
                quotient=abs(value)//abs(denominator)
                if (value<0)!=(denominator<0):quotient=-quotient
                expected.append(quotient & 255)
            self.assertEqual([a,b],expected)
