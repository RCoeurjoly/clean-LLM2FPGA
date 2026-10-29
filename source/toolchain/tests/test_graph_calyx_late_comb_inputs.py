"""Late trusted condition-input lifting under an explicit binary-control contract."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def iverilog():
    return os.environ.get('LLM2FPGA_IVERILOG', shutil.which('iverilog'))


def vvp():
    return str(Path(iverilog()).with_name('vvp'))


def fixture():
    return '''import "primitives/core.futil";
import "primitives/binary_operators.futil";
component main(value: 64) -> (out0: 64) {
      cells {
        a = std_reg(64); b = std_reg(64); result = std_reg(64); output_reg = std_reg(64); index = std_reg(64);
         lt_a = std_slt(64); lt_b = std_slt(64); lt_i = std_slt(64);
        inv = std_not(64); gate = std_slice(64, 1);
        add_three = std_add(64); add_five = std_add(64); add_one = std_add(64); add_index = std_add(64); final_add = std_add(64);

      }
      wires {
        out0 = output_reg.out; inv.in = value; gate.in = value;
        group load_a { a.in = value; a.write_en = 1'd1; load_a[done] = a.done; }
        group load_b { b.in = inv.out; b.write_en = 1'd1; load_b[done] = b.done; }
        group first_neg { result.in = 64'd17; result.write_en = 1'd1; first_neg[done] = result.done; }
        group first_pos { result.in = 64'd29; result.write_en = 1'd1; first_pos[done] = result.done; }
        comb group ca { lt_a.left = a.out; lt_a.right = 64'd0; }
        comb group cb { lt_b.left = b.out; lt_b.right = 64'd0; }
        group plus_three { add_three.left = result.out; add_three.right = 64'd3; result.in = add_three.out; result.write_en = 1'd1; plus_three[done] = result.done; }
        group plus_five { add_five.left = result.out; add_five.right = 64'd5; result.in = add_five.out; result.write_en = 1'd1; plus_five[done] = result.done; }
        group init_index { index.in = 64'd0; index.write_en = 1'd1; init_index[done] = index.done; }
        comb group ci { lt_i.left = index.out; lt_i.right = 64'd3; }
        group step {
          add_one.left = result.out; add_one.right = 64'd1;
          result.in = add_one.out; result.write_en = 1'd1;
          add_index.left = index.out; add_index.right = 64'd1;
          index.in = add_index.out; index.write_en = 1'd1;
          step[done] = result.done & index.done ? 1'd1;
        }
        group finish { final_add.left = value; final_add.right = result.out;
          output_reg.in = final_add.out; output_reg.write_en = 1'd1; finish[done] = output_reg.done; }

      }
      control { seq {
        load_a; if lt_a.out with ca { first_neg; } else { first_pos; }
        load_b; if lt_b.out with cb { plus_three; } else { plus_five; }
        init_index; while lt_i.out with ci { step; } finish;
      } }
    }
'''


class LateCombInputTests(unittest.TestCase):


    def test_complete_pipeline_preserves_cpu_results_and_public_cycle_traces(self):
        tool=Path(os.environ['LLM2FPGA_CALYX_TOOL']);library=tool.parent.parent/'share/calyx'
        # Explicit test settings, also runnable in the native backend sandbox.
        resource_passes=('-x','cell-share:bounds=1,-1,-1',
            '-x','cell-share:retire-dead-cells=true',
            '-x','simplify-with-control:retire-dead-groups=true',
            '-x','static-promotion:retire-dead-groups=true')
        seeds=[0,1,-1,(1<<63)-1,-(1<<63),0x123456788765FEDC]
        traces=[]
        for enabled in [False,True]:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);(root/'source.futil').write_text(fixture())
                args=[str(tool),'source.futil','-p','external',*resource_passes,'--synthesis','--nested','-l',str(library)]
                if enabled:args+=['-x','late-comb-inputs:assume-binary-control=true']
                result=subprocess.run([*args,'-b','verilog','-o','kernel.sv'],cwd=root,text=True,capture_output=True,timeout=90)
                self.assertEqual(result.returncode,0,result.stderr)
                checks=[]
                for seed in seeds:
                    bits=seed&((1<<64)-1);signed=bits if bits<(1<<63) else bits-(1<<64)
                    expected=(bits+(25 if signed<0 else 35))&((1<<64)-1)
                    checks.append(f'''reset=1;go=0;repeat(3)@(negedge clk);value=64'h{bits:x};reset=0;go=1;cycles=0;
                      while(!done&&cycles<300)begin @(negedge clk);cycles=cycles+1;end
                      if(!done||out0!==64'h{expected:x})$fatal(1,"CPU mismatch input=%h output=%h",value,out0);
                      go=0;repeat(3)@(negedge clk);''')
                (root/'tb.sv').write_text('''module tb;reg clk=0,reset=1,go=0;reg[63:0]value=0;
                  wire done;wire[63:0]out0;integer cycles=0;main dut(.*);always #5 clk=~clk;
                  always @(negedge clk)$display("TRACE %h %b%b%b %h",value,reset,go,done,out0);
                  initial begin '''+''.join(checks)+''' $display("PASS CPU");$finish;end endmodule''')
                build=subprocess.run([iverilog(),'-g2012','-s','tb','-o','sim','tb.sv','kernel.sv'],cwd=root,text=True,capture_output=True,timeout=60)
                self.assertEqual(build.returncode,0,build.stderr)
                sim=subprocess.run([vvp(),'sim'],cwd=root,text=True,capture_output=True,timeout=60)
                self.assertEqual(sim.returncode,0,sim.stdout+sim.stderr)
                self.assertIn('PASS CPU',sim.stdout)
                traces.append([l for l in sim.stdout.splitlines() if l.startswith('TRACE ')])
        self.assertEqual(traces[0],traces[1])

    def test_x_activation_is_outside_the_explicit_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'tb.sv').write_text('''module tb;reg g=1'bx;wire a,b;
              assign a=g?~(g?1'b1:1'b0):1'b0;
              assign b=g?~1'b1:1'b0;
              initial begin #1;if(a!==1'bx||b!==1'b0)$fatal(1,"expected X-control distinction");
                $display("PASS explicit X limit");$finish;end endmodule''')
            build=subprocess.run([iverilog(),'-g2012','-o','sim','tb.sv'],cwd=root,text=True,capture_output=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            sim=subprocess.run([vvp(),'sim'],cwd=root,text=True,capture_output=True,timeout=30)
            self.assertEqual(sim.returncode,0,sim.stdout+sim.stderr)
            self.assertIn('PASS explicit X limit',sim.stdout)


if __name__=='__main__':unittest.main()
