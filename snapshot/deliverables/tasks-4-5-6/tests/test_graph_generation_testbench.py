"""Public-interface verification must reject completion without written results."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class GenerationTestbenchTests(unittest.TestCase):
    def load(self, name='emit_generation_testbench'):
        path = Path(__file__).resolve().parents[1] / 'scripts/comparison/graph_rtl_runtime.py'
        spec = importlib.util.spec_from_file_location('runtime', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        builder = getattr(module, name, None)
        self.assertIsNotNone(builder, 'public verification testbench builder missing')
        return builder

    def test_completion_without_output_writes_fails_simulation(self):
        builder = self.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'tb.sv').write_text(builder(3, max_cycles=20))
            ports = ['input clk, reset, go', 'output done']
            assignments = ['assign done = go;']
            for role, bits in [('prompt', 2), ('generated_tokens', 4), ('last_logits', 2)]:
                ports += [f'output [{bits-1}:0] {role}_addr0',
                          f'output {role}_clk, {role}_reset, {role}_content_en, {role}_write_en',
                          f'output [63:0] {role}_write_data', f'input [63:0] {role}_read_data',
                          f'input {role}_done']
                assignments += [f'assign {role}_clk=clk;', f'assign {role}_reset=reset;',
                    f'assign {role}_content_en=0;', f'assign {role}_write_en=0;',
                    f'assign {role}_addr0=0;', f'assign {role}_write_data=0;']
            (root/'empty_device.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(assignments)+'\nendmodule\n')
            (root/'prompt.hex').write_text('1\n2\n3\n4\n')
            tool = shutil.which('iverilog'); self.assertIsNotNone(tool)
            subprocess.run([tool,'-g2012','-s','tb','-o',str(root/'sim'),str(root/'tb.sv'),str(root/'empty_device.sv')],check=True,capture_output=True)
            run = subprocess.run([shutil.which('vvp'),str(root/'sim')],cwd=root,capture_output=True,text=True)
            self.assertNotEqual(run.returncode,0)
            self.assertIn('unwritten generated token',run.stdout)

    def test_invalid_geometry_and_cycle_limits_are_rejected(self):
        builder = self.load()
        for words in (0, -1, True, 3.5):
            with self.assertRaises(ValueError): builder(words)
        for cycles in (0, -1, True, 2**63):
            with self.assertRaises(ValueError): builder(3,max_cycles=cycles)

    def test_known_public_requests_pass_and_unknown_or_out_of_bounds_requests_fail(self):
        # Removing either control/address check must admit an invalid request;
        # a tool false positive must not reject a completely known request.
        from scripts.comparison.graph_rtl_runtime import _PUBLIC_MEMORY_MODULE
        bench='''module tb;reg clk=0;always #5 clk=~clk;
reg reset=1,en=0,we=1;reg[1:0]addr=0;wire[63:0]data;wire done;integer mode=0;
verification_memory #(.WORDS(3),.ADDR(2)) dut(.clk(clk),.reset(reset),
.content_en(en),.write_en(we),.addr0(addr),.write_data(64'h17),.read_data(data),.done(done));
initial begin
if($value$plusargs("CASE=%d",mode))begin end
repeat(2)@(negedge clk);reset=0;en=1;
case(mode) 0:addr=0;1:addr=2;2:addr=3;3:addr=2'bxx;4:addr=2'bzz;
5:we=1'bx;6:we=1'bz;endcase
repeat(3)@(negedge clk);$display("OK %016h",dut.memory[addr]);$finish;
end endmodule'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tb.sv').write_text(_PUBLIC_MEMORY_MODULE+bench)
            tool=shutil.which('iverilog');self.assertIsNotNone(tool)
            subprocess.run([tool,'-g2012','-s','tb','-o',str(root/'sim'),str(root/'tb.sv')],
                check=True,capture_output=True)
            for mode in range(7):
                with self.subTest(mode=mode):
                    result=subprocess.run([shutil.which('vvp'),str(root/'sim'),f'+CASE={mode}'],
                        cwd=root,capture_output=True,text=True,timeout=10)
                    if mode<2:
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('OK 0000000000000017',result.stdout)
                    else:
                        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('invalid public memory request',result.stdout)

    def test_loaded_geometry_is_rejected_before_emission(self):
        builder = self.load('emit_loaded_generation_testbench')
        for frames in (0, -1, True, 2**25+1):
            with self.assertRaises(ValueError): builder(3, frames)

    def test_native_peer_accepts_both_command_data_orders_and_public_results(self):
        builder=self.load('emit_loaded_generation_testbench')
        ports=['input clk,reset,go,ui_clk,controller_ready,input_valid,cmd_ready,wdata_ready,rdata_valid',
               'input [575:0] input_data,rdata',
               'output done,fault,input_ready,ui_loaded,ui_load_fault,core_fault,core_memory_ready,cmd_valid,cmd_we,wdata_valid,rdata_ready',
               'output [24:0] frame_index,cmd_addr',
               'output [575:0] wdata','output [71:0] wdata_we']
        for role,bits in [('prompt',2),('generated_tokens',4),('last_logits',2)]:
            ports += [f'output [{bits-1}:0] {role}_addr0',
                      f'output {role}_clk,{role}_reset,{role}_content_en,{role}_write_en',
                      f'output [63:0] {role}_write_data',f'input [63:0] {role}_read_data',f'input {role}_done']
        # Authored harness fixture: results depend on the returned native word.
        # It is never a production compiler input or a model-equivalence proof.
        body='''
 reg[3:0]us=0,cs=0;reg command_seen=0,data_seen=0,loaded=0,value_ready=0,finished=0;
 reg[575:0]frame=0;reg[63:0]word=0;integer index=0;
 assign fault=0;assign ui_load_fault=0;assign core_fault=0;
 assign ui_loaded=loaded;assign core_memory_ready=loaded;assign done=finished;
 assign input_ready=us==0&&controller_ready;assign frame_index=0;
 assign cmd_valid=(us==1&&!command_seen)||us==2||us==5;
 assign cmd_we=us==1;assign cmd_addr=0;
 assign wdata_valid=us==1&&!data_seen;assign wdata=frame;assign wdata_we={72{1'b1}};
 assign rdata_ready=us==3||us==6;
 always@(posedge ui_clk)if(reset)begin
  us<=0;command_seen<=0;data_seen<=0;loaded<=0;value_ready<=0;
 end else case(us)
  0:if(input_valid&&input_ready)begin frame<=input_data;us<=1;end
  1:begin
   if(cmd_valid&&cmd_ready)command_seen<=1;
   if(wdata_valid&&wdata_ready)data_seen<=1;
   if((command_seen||(cmd_valid&&cmd_ready))&&(data_seen||(wdata_valid&&wdata_ready)))us<=2;
  end
  2:if(cmd_valid&&cmd_ready)us<=3;
  3:if(rdata_valid&&rdata_ready)begin loaded<=1;us<=4;end
  4:if(go)us<=5;
  5:if(cmd_valid&&cmd_ready)us<=6;
  6:if(rdata_valid&&rdata_ready)begin word<=rdata[63:0];value_ready<=1;us<=7;end
 endcase
 assign prompt_clk=clk;assign prompt_reset=reset;assign prompt_addr0=0;
 assign prompt_content_en=0;assign prompt_write_en=0;assign prompt_write_data=0;
 assign generated_tokens_clk=clk;assign generated_tokens_reset=reset;
 assign generated_tokens_addr0=index[3:0];assign generated_tokens_content_en=cs==1;
 assign generated_tokens_write_en=cs==1;assign generated_tokens_write_data=word+index;
 assign last_logits_clk=clk;assign last_logits_reset=reset;
 assign last_logits_addr0=index[1:0];assign last_logits_content_en=cs==3;
 assign last_logits_write_en=cs==3;assign last_logits_write_data=word-index;
 always@(posedge clk)if(reset)begin cs<=0;index<=0;finished<=0;end else case(cs)
  0:if(go&&value_ready)cs<=1;
  1:if(generated_tokens_done)cs<=2;
  2:if(!generated_tokens_done)begin
   if(index==15)begin index<=0;cs<=3;end else begin index<=index+1;cs<=1;end
  end
  3:if(last_logits_done)cs<=4;
  4:if(!last_logits_done)begin
   if(index==2)begin finished<=1;cs<=5;end else begin index<=index+1;cs<=3;end
  end
 endcase
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tb.sv').write_text(builder(3,1,max_cycles=1000))
            (root/'device.sv').write_text('module graph_ddr_bank('+','.join(ports)+');\n'+body+'\nendmodule\n')
            (root/'prompt.hex').write_text('1\n2\n3\n4\n')
            (root/'frames.hex').write_text('0000000000000011\n')
            tool=shutil.which('iverilog');self.assertIsNotNone(tool)
            subprocess.run([tool,'-g2012','-s','tb','-o',str(root/'sim'),str(root/'tb.sv'),str(root/'device.sv')],check=True,capture_output=True)
            for order in (0,1):
                with self.subTest(order=order):
                    result=subprocess.run([shutil.which('vvp'),str(root/'sim'),f'+ORDER={order}','+DELAY=7'],cwd=root,capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    tokens=[int(line.split()[2],16) for line in result.stdout.splitlines() if line.startswith('TOKEN ')]
                    logits=[int(line.split()[2],16) for line in result.stdout.splitlines() if line.startswith('LOGIT ')]
                    self.assertEqual(tokens,list(range(17,33)))
                    self.assertEqual(logits,[17,16,15])
                    self.assertIn('NATIVE 1 1 1',result.stdout)

    def test_claimed_loading_without_native_transactions_fails(self):
        builder = self.load('emit_loaded_generation_testbench')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tb.sv').write_text(builder(3, 1, max_cycles=100))
            ports=['input clk,reset,go,ui_clk,controller_ready,input_valid,cmd_ready,wdata_ready,rdata_valid',
                   'input [575:0] input_data,rdata',
                   'output done,fault,input_ready,ui_loaded,ui_load_fault,core_fault,core_memory_ready,cmd_valid,cmd_we,wdata_valid,rdata_ready',
                   'output [24:0] frame_index,cmd_addr',
                   'output [575:0] wdata','output [71:0] wdata_we']
            assignments=['assign done=go;', 'assign ui_loaded=controller_ready;',
                         'assign core_memory_ready=controller_ready;',
                         'assign fault=0;assign ui_load_fault=0;assign core_fault=0;',
                         'assign input_ready=1;assign frame_index=0;',
                         'assign cmd_valid=0;assign cmd_we=0;assign cmd_addr=0;',
                         'assign wdata_valid=0;assign wdata=0;assign wdata_we=0;assign rdata_ready=0;']
            for role,bits in [('prompt',2),('generated_tokens',4),('last_logits',2)]:
                ports += [f'output [{bits-1}:0] {role}_addr0',
                          f'output {role}_clk,{role}_reset,{role}_content_en,{role}_write_en',
                          f'output [63:0] {role}_write_data',f'input [63:0] {role}_read_data',f'input {role}_done']
                assignments += [f'assign {role}_clk=clk;assign {role}_reset=reset;',
                    f'assign {role}_content_en=0;assign {role}_write_en=0;',
                    f'assign {role}_addr0=0;assign {role}_write_data=0;']
            (root/'device.sv').write_text('module graph_ddr_bank('+','.join(ports)+');\n'+'\n'.join(assignments)+'\nendmodule\n')
            (root/'prompt.hex').write_text('1\n2\n3\n4\n')
            (root/'frames.hex').write_text('0\n')
            tool=shutil.which('iverilog');self.assertIsNotNone(tool)
            subprocess.run([tool,'-g2012','-s','tb','-o',str(root/'sim'),str(root/'tb.sv'),str(root/'device.sv')],check=True,capture_output=True)
            result=subprocess.run([shutil.which('vvp'),str(root/'sim')],cwd=root,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('unverified image loading',result.stdout)
