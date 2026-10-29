"""On-chip synthesis must read the authenticated images from its own directory."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt
from scripts.pipeline.map_graph_core_xc7 import coarse_checkpoint


@unittest.skipUnless(shutil.which('yosys'), 'requires native Yosys')
class OnchipMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.yosys = Path(shutil.which('yosys')).resolve()
        self.image = self.root/'weights.hex'
        self.image.write_text('01\n80\nff\n07\n')
        self.source = self.root/'source.sv'
        self.source.write_text('module main(input [1:0] a,output [7:0] q); '
            'reg [7:0] mem[0:3]; initial $readmemh("weights.hex",mem); '
            'assign q=mem[a]; endmodule\n')

    def parent(self, extra=()):
        python = Path(sys.executable).resolve()
        paths = (self.source, self.image, *extra)
        parent = self.root/'parent'
        run_stage(StageSpec('initialized-rom-input',
            (str(python), '-c', 'import shutil;shutil.copyfile('+
             repr(str(self.source))+',"kernel.sv")'),
            {str(p): digest(p) for p in paths}, {str(python): digest(python)},
            {}, ('kernel.sv',)), parent)
        return parent

    def test_narrow_lut_rom_maps_and_reads_all_signed_words(self):
        from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources
        raw = hashlib.shake_256(b'variable-address-ROM').digest(64*4)
        words = [int.from_bytes(raw[i*4:i*4+4], 'little') & ((1<<17)-1) for i in range(64)]
        words[0:2] = [1<<16, (1<<16)-1]
        signed = [v-(1<<17) if v & (1<<16) else v for v in words]
        self.image.write_text(''.join(f'{v & ((1<<64)-1):016x}\n' for v in signed))
        primitive = emit_memory_sources([
            MemorySpec('fixture',64,64,'lutram',2,False,self.image.name)],
            include_narrow_rom=True)['sv']
        self.source.write_text(primitive + '''
module main(input clk,reset,content_en,input [5:0] address,
 output [63:0] value,output done);
 closure_lutram_memory_narrow #(.WIDTH(64),.SIZE(64),.IDX_SIZE(6),
 .INIT_ID(1),.STORAGE_WIDTH(17),.SIGN_EXTEND(1)) rom(
 .clk(clk),.reset(reset),.content_en(content_en),.addr0(address),
 .write_en(1'b0),.write_data(64'b0),.read_data(value),.done(done));
endmodule
''')
        result = subprocess.run([str(self.yosys), '-Q','-T','-p',
            'read_verilog -sv source.sv; synth_xilinx -family xc7 -top main -flatten -noiopad -noclkbuf; '
            'check -assert; scc -expect 0; rename main mapped_main; '
            'write_verilog -noattr mapped.sv'],
            cwd=self.root,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(shutil.which('iverilog') and shutil.which('vvp'), 'Icarus required')
        (self.root/'tb.sv').write_text('''
module tb;
 reg clk=0; always #5 clk=~clk;
 reg reset=1,content_en=0; reg [5:0] address=0;
 wire [63:0] original_value,mapped_value; wire original_done,mapped_done;
 reg [63:0] expected [0:63]; integer i;
 main original(clk,reset,content_en,address,original_value,original_done);
 mapped_main mapped(clk,reset,content_en,address,mapped_value,mapped_done);
 initial begin
  $readmemh("weights.hex",expected);
  repeat(3) @(negedge clk); reset=0;
  for(i=0;i<64;i=i+1) begin
   address=i; content_en=1; @(negedge clk); content_en=0;
   if(original_done!==0 || mapped_done!==0) $fatal(1,"early completion");
   @(negedge clk);
   if(original_done!==1 || mapped_done!==1 || original_value!==expected[i] || mapped_value!==expected[i])
    $fatal(1,"signed ROM mismatch address %d",i);
   @(negedge clk);
   if(original_done!==0 || mapped_done!==0) $fatal(1,"completion not retired");
  end
  reset=1; @(negedge clk);
  if(original_value!==0 || mapped_value!==0) $fatal(1,"reset mismatch");
  $display("PASS:64 signed ROM words and two-cycle interface"); $finish;
 end
 initial begin #10000; $fatal(1,"timeout"); end
endmodule
''')
        library=self.yosys.parent.parent/'share/yosys/xilinx/cells_sim.v'
        built=subprocess.run(['iverilog','-g2012','-s','tb','-o','simulator',
            'source.sv','mapped.sv','tb.sv',str(library)],cwd=self.root,
            capture_output=True,text=True,timeout=30)
        self.assertEqual(built.returncode,0,built.stderr)
        checked=subprocess.run(['vvp','simulator'],cwd=self.root,capture_output=True,text=True,timeout=30)
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
        self.assertIn('PASS:64 signed ROM words',checked.stdout)

    def test_native_checkpoint_rejects_changed_staged_initializer(self):
        output = self.root/'coarse'
        coarse_checkpoint(self.parent(), 'kernel.sv', self.yosys, output)
        verify_stage_receipt(output)
        (output/'weights.hex').write_text('00\n00\n00\n00\n')
        with self.assertRaisesRegex(ValueError, 'dependency changed'):
            verify_stage_receipt(output)

    def test_conflicting_authenticated_basenames_are_rejected(self):
        other = self.root/'other'/'weights.hex'
        other.parent.mkdir()
        other.write_text('00\n00\n00\n00\n')
        with self.assertRaisesRegex(ValueError, 'conflicting initializer'):
            coarse_checkpoint(self.parent((other,)), 'kernel.sv', self.yosys,
                              self.root/'coarse')
