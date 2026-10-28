"""Memory contracts are executable at the primitive boundary."""
from pathlib import Path
import importlib
import tempfile
import os
import shutil
import subprocess
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ClosureMemoryTest(unittest.TestCase):
    def policy(self):
        self.assertTrue((ROOT / 'scripts/pipeline/memory_policy.py').is_file(), 'explicit memory policy missing')
        return importlib.import_module('scripts.pipeline.memory_policy')

    def test_two_rom_changes_only_projection_placement(self):
        policy = self.policy()
        spec = policy.MemorySpec('c_proj_weight_codes_i8', 8, 131072, 'auto', 0, False, 'image-c_proj_weight_codes_i8.hex')
        one = policy.select_memory(spec, 'one-rom')
        two = policy.select_memory(spec, 'two-rom')
        self.assertEqual(one.implementation, 'lutram')
        self.assertEqual(two.implementation, 'bram')
        self.assertEqual(one.read_latency, two.read_latency)

    def test_image_geometry_and_width_fail_closed(self):
        policy = self.policy()
        spec = policy.MemorySpec('weights', 9, 2, 'bram', 2, False, 'image-weights.hex')
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / spec.init_file
            for content in ('', '001\n', '001\n200\n', '001\nzz0\n'):
                image.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    policy.validate_image(spec, Path(tmp))
            image.write_text('001\n1ff\n')
            self.assertEqual(policy.validate_image(spec, Path(tmp))['elements'], 2)

    def test_initializer_is_emitted_in_primitive_source(self):
        policy = self.policy()
        spec = policy.MemorySpec('weights', 8, 2, 'bram', 2, False, 'image-weights.hex')
        emitted = policy.emit_memory_sources([spec])
        self.assertIn('$readmemh("image-weights.hex", mem)', emitted['sv'])
        self.assertIn('@interval(2)', emitted['futil'])
        self.assertEqual(emitted['bindings']['weights']['primitive'], 'closure_bram_memory')
        self.assertEqual(emitted['bindings']['weights']['parameters'], [8, 2, 1, 1, 0])

    def test_unsafe_image_names_and_unbound_rom_are_rejected(self):
        policy = self.policy()
        with self.assertRaises(ValueError):
            policy.emit_memory_sources([policy.MemorySpec('weights', 8, 2, 'bram', 2, False, None)])
        with self.assertRaises(ValueError):
            policy.emit_memory_sources([policy.MemorySpec('weights', 8, 2, 'bram', 2, False, '../escape.hex')])

    def test_read_write_pipeline_reset_and_image_in_simulation(self):
        policy = self.policy()
        compiler = os.environ.get('LLM2FPGA_IVERILOG') or shutil.which('iverilog')
        self.assertIsNotNone(compiler, 'memory protocol gate requires iverilog')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            spec = policy.MemorySpec('weights', 8, 2, 'bram', 2, True, 'image-weights.hex')
            (output / spec.init_file).write_text('11\n22\n')
            (output / 'memory.sv').write_text(policy.emit_memory_sources([spec])['sv'])
            (output / 'test.sv').write_text(r"""
module test;
  reg clk=0, reset=1, en=0, we=0, addr=0;
  reg [7:0] wd=0;
  wire [7:0] rd;
  wire done;
  closure_bram_memory #(.WIDTH(8),.SIZE(2),.IDX_SIZE(1),.INIT_ID(1)) dut
      (.clk(clk),.reset(reset),.addr0(addr),.content_en(en),.write_en(we),.write_data(wd),.read_data(rd),.done(done));
  task tick; begin #5;clk=1;#1;clk=0;#4; end endtask
  initial begin
    tick; reset=0;
    en=1;addr=0;tick;if(done) $fatal(1,"premature done");
    en=0;tick;if(!done || rd!==8'h11) $fatal(1,"pulse read/image");
    tick;if(done) $fatal(1,"done must retire");
    en=1;addr=0;tick;addr=1;tick;if(rd!==8'h11 || !done) $fatal(1,"first consecutive read");
    en=0;tick;if(rd!==8'h22 || !done) $fatal(1,"second consecutive read");
    en=1;we=1;addr=0;wd=8'h33;tick;en=0;we=0;tick;if(!done) $fatal(1,"write completion");
    en=1;addr=0;tick;en=0;tick;if(rd!==8'h33) $fatal(1,"written value");
    en=1;addr=1;tick;reset=1;tick;if(done || rd!==0) $fatal(1,"reset cancellation");
    reset=0;en=0;tick;if(done) $fatal(1,"reset pending request");
    $display("PASS memory protocol");$finish;
  end
endmodule
""")
            subprocess.run([compiler, '-g2012', '-s', 'test', '-o', str(output / 'sim'), 'memory.sv', 'test.sv'], cwd=output, check=True, capture_output=True, text=True)
            vvp = str(Path(compiler).with_name('vvp'))
            result = subprocess.run([vvp, str(output / 'sim')], cwd=output, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS memory protocol', result.stdout)

    def test_non_power_of_two_address_bounds_in_simulation(self):
        policy = self.policy()
        compiler = os.environ.get('LLM2FPGA_IVERILOG') or shutil.which('iverilog')
        self.assertIsNotNone(compiler, 'bounds gate requires iverilog')
        for kind, latency in (('register', 1), ('bram', 2), ('lutram', 2)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                spec = policy.MemorySpec('weights', 8, 3, kind, latency, False, 'weights.hex')
                (output / 'weights.hex').write_text('11\n22\n33\n')
                (output / 'memory.sv').write_text(policy.emit_memory_sources([spec])['sv'])
                (output / 'test.sv').write_text(f"""
module test;
  reg clk=0, reset=1, en=0;
  reg [1:0] addr=0;
  wire [7:0] rd;
  wire done;
  closure_{kind}_memory #(.WIDTH(8),.SIZE(3),.IDX_SIZE(2),.INIT_ID(1),.WRITABLE(0)) dut
    (.clk(clk),.reset(reset),.addr0(addr),.content_en(en),.write_en(1'b0),
     .write_data(8'b0),.read_data(rd),.done(done));
  task tick; begin #5;clk=1;#1;clk=0;#4; end endtask
  initial begin
    tick;reset=0;addr=2;en=1;tick;en=0;
    if ({latency}==2) tick;
    if (!done || rd!==8'h33) $fatal(1,"last valid address");
    tick;addr=3;en=1;tick;
    $display("BOUNDS CHECK MISSING");$finish;
  end
endmodule
""")
                subprocess.run([compiler, '-g2012', '-s', 'test', '-o', 'sim',
                                'memory.sv', 'test.sv'], cwd=output, check=True,
                               capture_output=True, text=True)
                result = subprocess.run([str(Path(compiler).with_name('vvp')), 'sim'],
                                        cwd=output, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('memory address out of range', result.stdout + result.stderr)
                self.assertNotIn('BOUNDS CHECK MISSING', result.stdout)

    def test_yosys_elaboration_retains_initialized_memory_words(self):
        policy = self.policy()
        yosys = os.environ.get('LLM2FPGA_YOSYS') or shutil.which('yosys')
        self.assertIsNotNone(yosys, 'memory image gate requires yosys')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            spec = policy.MemorySpec('weights', 8, 2, 'bram', 2, False, 'image-weights.hex')
            (output / spec.init_file).write_text('11\n22\n')
            (output / 'memory.sv').write_text(policy.emit_memory_sources([spec])['sv'])
            (output / 'top.sv').write_text('module top(input clk, reset, en, addr, output [7:0] rd, output done); closure_bram_memory #(.WIDTH(8),.SIZE(2),.IDX_SIZE(1),.INIT_ID(1),.WRITABLE(0)) dut (.clk(clk),.reset(reset),.content_en(en),.addr0(addr),.write_en(1\'b0),.write_data(8\'b0),.read_data(rd),.done(done)); endmodule')
            subprocess.run([yosys, '-Q', '-T', '-p', 'read_verilog -sv -D SYNTHESIS memory.sv top.sv; hierarchy -top top; proc; memory_collect; write_json design.json'], cwd=output, check=True, capture_output=True, text=True)
            design = json.loads((output / 'design.json').read_text())
            memories = [cell for module in design['modules'].values() for cell in module['cells'].values() if cell['type'] == '$mem_v2' and cell['parameters']['SIZE'] == format(2, '032b')]
            self.assertEqual(len(memories), 1)
            self.assertEqual(int(memories[0]['parameters']['INIT'], 2), 0x2211)

if __name__ == '__main__':
    unittest.main()
