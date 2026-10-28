import json,os,subprocess,tempfile,unittest
from pathlib import Path
Y=os.environ.get('YOSYS','yosys')
class Alias(unittest.TestCase):
 def run_design(self,source,selection="",assert_valid=True):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'input.v').write_text(source);args=[Y,'-Q','-T']
   if os.environ.get('PLUGIN'):args+=['-m',os.environ['PLUGIN']]
   command=os.environ.get('PASS','opt_whole_wire_aliases')+' '+selection
   check='check -assert' if assert_valid else 'check'
   r=subprocess.run(args+['-p',f'read_verilog {p}/input.v; hierarchy -top main; proc; {command}; select *; {check}; write_json {p}/out.json'],capture_output=True,text=True,timeout=30);self.assertEqual(r.returncode,0,r.stderr)
   return json.loads((p/'out.json').read_text())['modules']['main']
 def test_retires_wide_alias_and_preserves_slice(self):
  m=self.run_design('module main(input [31:0] a,output [3:0] y);wire [31:0] alias;assign alias=a;assign y=alias[7:4];endmodule')
  self.assertNotIn('alias',m['netnames']);self.assertEqual(m['ports']['y']['bits'],m['ports']['a']['bits'][4:8])
 def test_keep_retained(self):
  m=self.run_design('module main(input [31:0] a,output [31:0] y);(* keep=1 *)wire [31:0] alias;assign alias=a;assign y=alias;endmodule');self.assertIn('alias',m['netnames'])
 def test_port_retained(self):
  m=self.run_design('module main(input [31:0] a,output [31:0] alias);assign alias=a;endmodule');self.assertIn('alias',m['ports'])
 def test_partial_assignment_retained(self):
  m=self.run_design("module main(input [31:0] a,output [3:0] y);wire [31:0] alias;assign alias[15:0]=a[15:0];assign alias[31:16]=16'b0;assign y=alias[7:4];endmodule");self.assertIn('alias',m['netnames'])
 def test_chain_and_cell_inputs(self):
  m=self.run_design('module main(input [7:0] a,output [7:0] y);wire [7:0] first,second;assign first=a;assign second=first;assign y=~second;endmodule')
  self.assertNotIn('first',m['netnames']);self.assertNotIn('second',m['netnames']);cell=next(iter(m['cells'].values()));self.assertEqual(cell['connections']['A'],m['ports']['a']['bits'])
 def test_signed_preserved(self):
  m=self.run_design('module main(input signed [7:0] a,output signed [15:0] y);wire signed [7:0] alias;assign alias=a;assign y=alias;endmodule')
  self.assertNotIn('alias',m['netnames']);a=m['ports']['a']['bits'];self.assertEqual(m['ports']['y']['bits'],a+[a[-1]]*8)
 def test_initialized_wire_retained(self):
  m=self.run_design("module main(input clk,input [7:0] a,output [7:0] y);reg [7:0] alias=8'h01;always @(posedge clk)alias<=a;assign y=alias;endmodule")
  self.assertIn('alias',m['netnames'])
 def test_cell_driven_source_preserved(self):
  m=self.run_design('module main(input [7:0] a,output [7:0] y);wire [7:0] driven,alias;assign driven=~a;assign alias=driven;assign y=alias;endmodule')
  self.assertNotIn('alias',m['netnames']);self.assertEqual(len(m['cells']),1);cell=next(iter(m['cells'].values()));self.assertEqual(cell['connections']['Y'],m['ports']['y']['bits'])
 def test_cycle_retained(self):
  m=self.run_design('module main(output y);wire first,second;assign first=second;assign second=first;assign y=first;endmodule',assert_valid=False)
  self.assertIn('first',m['netnames']);self.assertIn('second',m['netnames'])
 def test_multiple_assignments_retained(self):
  m=self.run_design('module main(input a,b,output y);wire alias;assign alias=a;assign alias=b;assign y=alias;endmodule',assert_valid=False)
  self.assertIn('alias',m['netnames'])
 def test_cell_driver_conflict_retained(self):
  m=self.run_design('module main(input a,b,output y);wire alias;assign alias=~a;assign alias=b;assign y=alias;endmodule',assert_valid=False)
  self.assertIn('alias',m['netnames']);self.assertEqual(len(m['cells']),1)
 def test_partial_selection_retained(self):
  m=self.run_design('module main(input a,output y);wire alias;assign alias=a;assign y=alias;endmodule',selection='main/w:alias')
  self.assertIn('alias',m['netnames'])
if __name__=='__main__':unittest.main()
