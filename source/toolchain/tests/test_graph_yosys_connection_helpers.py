import json,os,subprocess,tempfile,unittest
from pathlib import Path
Y=os.environ.get('YOSYS','yosys')
class Connections(unittest.TestCase):
 def run_design(self,body,selection='main/t:*helper*',assert_valid=True):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'input.v').write_text(body)
   args=[Y,'-Q','-T']
   if os.environ.get('PLUGIN'):args+=['-m',os.environ['PLUGIN']]
   command=os.environ.get('PASS','opt_connection_helpers')
   check='check -assert' if assert_valid else 'check'
   r=subprocess.run(args+['-p',f'read_verilog {p}/input.v; hierarchy -top main; proc; {command} {selection}; select *; {check}; write_json {p}/out.json'],capture_output=True,text=True,timeout=30)
   self.assertEqual(r.returncode,0,r.stderr)
   return json.loads((p/'out.json').read_text())['modules']['main']
 def test_slice_removed_and_connection_exact(self):
  m=self.run_design('module helper(input [7:0] a,output [1:0] y);assign y=a[5:4];endmodule module main(input [7:0] a,output [1:0] y);helper h(a,y);endmodule')
  self.assertFalse(m['cells']);self.assertEqual(m['ports']['y']['bits'],m['ports']['a']['bits'][4:6])
 def test_logic_not_removed(self):
  m=self.run_design('module helper(input a,output y);assign y=~a;endmodule module main(input a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_keep_hierarchy_retained(self):
  m=self.run_design('module helper(input a,output y);assign y=a;endmodule module main(input a,output y);(* keep_hierarchy=1 *)helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_constants_and_permutation(self):
  m=self.run_design("module helper(input [3:0] a,output [3:0] y);assign y={1'b1,a[0],1'b0,a[3]};endmodule module main(input [3:0] a,output [3:0] y);helper h(a,y);endmodule")
  self.assertFalse(m['cells']);a=m['ports']['a']['bits'];self.assertEqual(m['ports']['y']['bits'],[a[3],'0',a[0],'1'])
 def test_unselected_retained(self):
  m=self.run_design('module helper(input a,output y);assign y=a;endmodule module main(input a,output y);helper h(a,y);endmodule','main/c:absent')
  self.assertEqual(len(m['cells']),1)
 def test_state_retained(self):
  m=self.run_design('module helper(input clk,a,output reg y);always @(posedge clk)y<=a;endmodule module main(input clk,a,output y);helper h(clk,a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_undriven_output_retained(self):
  m=self.run_design('module helper(input a,output y);endmodule module main(input a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_module_keep_retained(self):
  m=self.run_design('(* keep_hierarchy=1 *)module helper(input a,output y);assign y=a;endmodule module main(input a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_whitebox_retained(self):
  m=self.run_design('(* whitebox=1 *)module helper(input a,output y);assign y=a;endmodule module main(input a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_blackbox_retained(self):
  m=self.run_design('(* blackbox=1 *)module helper(input a,output y);endmodule module main(input a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_inout_retained(self):
  m=self.run_design('module helper(inout a,output y);assign y=a;endmodule module main(inout a,output y);helper h(a,y);endmodule')
  self.assertEqual(len(m['cells']),1)
 def test_input_constraint_retained(self):
  m=self.run_design('module helper(input [1:0] a,output y);assign a[0]=a[1];assign y=a[0];endmodule module main(input [1:0] a,output y);helper h(a,y);endmodule',assert_valid=False)
  self.assertEqual(len(m['cells']),1)
if __name__=='__main__':unittest.main()
