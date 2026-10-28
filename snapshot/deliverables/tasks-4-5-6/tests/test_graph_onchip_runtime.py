"""CPU-checked runtime buffers for the native three-role graph interface."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy

from scripts.pipeline.memory_policy import emit_memory_sources

ROOT = Path(__file__).resolve().parents[1]
IVERILOG = shutil.which('iverilog')
VVP = shutil.which('vvp')


def native_interface():
    memories=[]
    for name,depth,addr,storage in [('prompt',4,2,'lutram'),
            ('generated_tokens',16,4,'lutram'),('last_logits',50257,16,'bram')]:
        ports=[dict(role=role,name=name+'_'+role,direction=direction,width=width)
               for role,direction,width in [('addr0','output',addr),('clk','output',1),
                ('reset','output',1),('content_en','output',1),('write_en','output',1),
                ('write_data','output',64),('read_data','input',64),('done','input',1)]]
        memories.append(dict(runtime_port=name,cell=name,component='main',width=64,
            depth=depth,address_width=addr,implementation=storage,read_latency=2,
            writable=True,ports=ports))
    return dict(schema_version=1,entrypoint='main',memories=memories,
        ports=[dict(name=name,direction=direction,width=1) for name,direction in
               [('clk','input'),('reset','input'),('go','input'),('done','output')]])


class OnchipInterfaceTests(unittest.TestCase):
    def test_exact_native_interface_and_changed_contract_rejection(self):
        from compiler.onchip_runtime import validate_onchip_interface
        document=native_interface()
        validate_onchip_interface(document)
        for field,value in [('width',32),('depth',5),('address_width',3),
                ('implementation','ddr3'),('writable',False),('read_latency',1),
                ('cell','unexpected'),('component','other')]:
            changed=deepcopy(document);changed['memories'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_onchip_interface(changed)
        changed=deepcopy(document);changed['memories'][0]['ports'][0]['width']=3
        with self.assertRaises(ValueError):validate_onchip_interface(changed)
        changed=deepcopy(document);changed['memories'].pop()
        with self.assertRaises(ValueError):validate_onchip_interface(changed)
        for value in (True,1.0):
            changed=deepcopy(document);changed['ports'][0]['width']=value
            with self.subTest(header_width=value),self.assertRaises(ValueError):
                validate_onchip_interface(changed)


def toy_core(words):
    ports = ['input clk,reset,go', 'output reg done=0']
    assignments = []
    for role, depth in [('prompt', 4), ('generated_tokens', 16), ('last_logits', words)]:
        addr = max(1, (depth-1).bit_length())
        ports += [f'output {role}_clk,{role}_reset',
                  f'output reg [{addr-1}:0] {role}_addr0=0',
                  f'output reg {role}_content_en=0,{role}_write_en=0',
                  f'output reg [63:0] {role}_write_data=0',
                  f'input [63:0] {role}_read_data', f'input {role}_done']
        assignments += [f'assign {role}_clk=clk; assign {role}_reset=reset;']
    return 'module graph_native_main('+','.join(ports)+');\n'+'\n'.join(assignments)+f'''
integer state=0,index=0; reg [63:0] seed=0;
always @(posedge clk) begin
 prompt_content_en<=0;generated_tokens_content_en<=0;last_logits_content_en<=0;
 if(reset)begin state<=0;done<=0;seed<=0;end
 else case(state)
 0:if(go)begin state<=1;index<=0;seed<=0;done<=0;end
 1:begin prompt_addr0<=index;prompt_content_en<=1;prompt_write_en<=0;state<=2;end
 2:if(prompt_done)begin
    seed<=seed+prompt_read_data;
    if(index==3)state<=3;else begin index<=index+1;state<=1;end
   end
 // A native caller may write all64 prompt bits; the adapter must retain them.
 3:begin prompt_addr0<=3;prompt_write_data<=64'hfedcba9876543210;
    prompt_write_en<=1;prompt_content_en<=1;state<=4;end
 4:if(prompt_done)begin index<=0;state<=5;end
 5:begin generated_tokens_addr0<=index;generated_tokens_write_data<=64'h8000000000000000+seed+index;
    generated_tokens_write_en<=1;generated_tokens_content_en<=1;state<=6;end
 6:if(generated_tokens_done)begin
    if(index==15)begin state<=7;index<=0;end else begin state<=5;index<=index+1;end
   end
 7:begin last_logits_addr0<=index;last_logits_write_data<=64'hfedcba9876543210^(seed+index);
    last_logits_write_en<=1;last_logits_content_en<=1;state<=8;end
 8:if(last_logits_done)begin
    if(index=={words-1})state<=9;else begin state<=7;index<=index+1;end
   end
 9:begin done<=1;if(!go)begin state<=0;done<=0;end end
 endcase
end
endmodule
'''


def bench(words):
    return f'''module tb;
reg clk=0,reset=1,start=0;always #5 clk=~clk;wire done;
reg [15:0] prompt_0=1,prompt_1=2,prompt_2=3,prompt_3=4;
reg [17:0] host_index=0;wire [15:0] host_piece;
integer cycles,run,index,part;reg[63:0] expected;integer seed;
graph_onchip_runtime #(.LOGIT_WORDS({words})) dut(.*);
task check_word(input integer address,input[63:0] value);
begin
 for(part=0;part<4;part=part+1)begin
  @(negedge clk);host_index=address*4+part;
  repeat(4)@(negedge clk);
  if(host_piece!==((value>>(part*16))&16'hffff))
   $fatal(1,"host word mismatch addr=%0d part=%0d got=%h expected=%h",address,part,host_piece,value);
 end
end endtask
initial begin
 repeat(3)@(negedge clk);reset=0;
 for(run=0;run<3;run=run+1)begin
  if(run==2)begin reset=1;repeat(3)@(negedge clk);reset=0;end
  prompt_0=1+run;prompt_1=2+run;prompt_2=3+run;prompt_3=4+run;seed=10+4*run;
  start=1;repeat(3)@(negedge clk);
  // Host CSR writes during a run must not change this run's prompt snapshot.
  prompt_0=100;prompt_1=100;prompt_2=100;prompt_3=100;
  cycles=0;while(!done && cycles<{words*10+1000})begin @(negedge clk);cycles=cycles+1;end
  if(!done)$fatal(1,"runtime did not complete");
  start=0;repeat(5)@(negedge clk);
  for(index=0;index<16;index=index+1)
   check_word(index,64'h8000000000000000+seed+index);
  check_word(16,64'hfedcba9876543210^seed);
  check_word(16+{words//2},64'hfedcba9876543210^(seed+{words//2}));
  check_word(16+{words-1},64'hfedcba9876543210^(seed+{words-1}));
  check_word(16+{words}+3,64'hfedcba9876543210);
 end
 $display("PASS runtime words={words} runs=3 tokens=48 boundary64=12");$finish;
end endmodule
'''


@unittest.skipUnless(IVERILOG and VVP, 'native Icarus required')
class OnchipRuntimeTests(unittest.TestCase):
    def run_runtime(self, words):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'memory.sv').write_text(emit_memory_sources([])['sv'])
            (root / 'core.sv').write_text(toy_core(words))
            (root / 'tb.sv').write_text(bench(words))
            command = [IVERILOG, '-g2012', '-s', 'tb', '-o', 'simulator',
                       'tb.sv', 'core.sv', 'memory.sv',
                       str(ROOT / 'rtl/graph_onchip_runtime.sv')]
            built = subprocess.run(command, cwd=root, capture_output=True,
                                   text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([VVP, 'simulator'], cwd=root, capture_output=True,
                                 text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stdout+ran.stderr)
            self.assertIn('PASS runtime', ran.stdout)

    def test_repeated_native_64bit_memory_transactions(self):
        self.run_runtime(13)

    def test_actual_depth_boundary_addresses(self):
        self.run_runtime(50257)

    def test_csr_clocked_memory_read_latency(self):
        source = '''module tb;
reg sys_clk=0,sys_rst=1;always #5 sys_clk=~sys_clk;
reg scan_drck=0,scan_reset=0,scan_sel=1,scan_shift=0;
reg scan_capture=0,scan_update=0,scan_tdi=0;wire scan_tdo;
wire core_start,core_reset,core_valid,tap_reset;wire[17:0]core_token_index;
wire[15:0]prompt_0,prompt_1,prompt_2,prompt_3;wire[63:0]response_word;
reg[17:0]address_q=0;reg[15:0]core_token_id=0;
always @(posedge sys_clk)begin address_q<=core_token_index;core_token_id<=16'h8000+address_q;end
tinystories_1m_jtag_csr #(.TOKEN_INDEX_WIDTH(18),.TOKEN_READ_LATENCY(3)) dut(
 .sys_clk(sys_clk),.sys_rst(sys_rst),.core_start(core_start),.core_reset(core_reset),
 .core_valid(core_valid),.core_token_index(core_token_index),.prompt_0(prompt_0),
 .prompt_1(prompt_1),.prompt_2(prompt_2),.prompt_3(prompt_3),.core_token_id(core_token_id),
 .core_done(1'b0),.model_init_done(1'b1),.response_word(response_word),.tap_reset(tap_reset),
 .scan_drck(scan_drck),.scan_reset(scan_reset),.scan_sel(scan_sel),.scan_shift(scan_shift),
 .scan_capture(scan_capture),.scan_update(scan_update),.scan_tdi(scan_tdi),.scan_tdo(scan_tdo));
task pulse;begin #2;scan_drck=1;#2;scan_drck=0;#2;end endtask
task command(input[63:0]word);
integer bit_index;begin
 scan_capture=1;pulse();scan_capture=0;scan_shift=1;
 for(bit_index=0;bit_index<64;bit_index=bit_index+1)begin scan_tdi=word[bit_index];pulse();end
 scan_shift=0;#2;scan_update=1;#2;scan_update=0;
 repeat(12)@(negedge sys_clk);
end endtask
initial begin
 @(negedge sys_clk);scan_reset=1;
 repeat(3)@(negedge sys_clk);scan_reset=0;sys_rst=0;
 command(64'h4000000000000011);
 if(response_word!==64'h8011)$fatal(1,"clocked read captured stale first address %h",response_word);
 command(64'h40000000000001f5);
 if(response_word!==64'h81f5)$fatal(1,"clocked read captured stale second address %h",response_word);
 $display("PASS CSR latency3");$finish;
end endmodule'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'tb.sv').write_text(source)
            built=subprocess.run([IVERILOG,'-g2012','-DBSCAN_SIM','-s','tb','-o','simulator',
                'tb.sv',str(ROOT/'rtl/tinystories_1m_jtag_csr.sv')],cwd=root,
                capture_output=True,text=True,timeout=30)
            self.assertEqual(built.returncode,0,built.stderr)
            ran=subprocess.run([VVP,'simulator'],cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            self.assertIn('PASS CSR latency3',ran.stdout)

    def test_complete_jtag_shell_reads_all_64bit_pieces(self):
        source = '''`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;wire[2:0]led;
reg scan_drck=0,scan_reset=0,scan_sel=1,scan_shift=0;
reg scan_capture=0,scan_update=0,scan_tdi=0;wire scan_tdo;
reg[63:0]reply,ignored;integer part;
graph_tinystories_ypcb_top dut(.*);
task pulse;begin #2;scan_drck=1;#2;scan_drck=0;#2;end endtask
task command(input[63:0]word,output[63:0]response);
integer bit_index;begin
 scan_capture=1;pulse();scan_capture=0;scan_shift=1;
 for(bit_index=0;bit_index<64;bit_index=bit_index+1)begin
  scan_tdi=word[bit_index];response[bit_index]=scan_tdo;pulse();
 end
 scan_shift=0;#2;scan_update=1;#2;scan_update=0;repeat(12)@(negedge clk);
end endtask
task check_word(input integer address,input[63:0]value);
integer piece;reg[63:0]response;begin
 for(piece=0;piece<4;piece=piece+1)begin
  command(64'h4000000000000000|(address*4+piece),ignored);
  command(64'h3000000000000000,response);
  if(response!==((value>>(piece*16))&16'hffff))
   $fatal(1,"JTAG read mismatch address=%0d piece=%0d got=%h",address,piece,response);
 end
end endtask
initial begin
 #2;scan_reset=1;repeat(4)@(negedge clk);scan_reset=0;repeat(6)@(negedge clk);
 command(64'h1000000000000001,ignored);command(64'h1000000000010002,ignored);
 command(64'h1000000000020003,ignored);command(64'h1000000000030004,ignored);
 command(64'h2000000000000000,ignored);
 repeat(300000)@(negedge clk);
 command(64'h3000000000000000,ignored);command(64'h3000000000000000,reply);
 if(reply[63:60]!==4'h2 || reply[59:57]!==3'b110)$fatal(1,"JTAG completion status mismatch %h",reply);
 check_word(0,64'h800000000000000a);
 check_word(15,64'h8000000000000019);
 check_word(16,64'hfedcba9876543210^64'd10);
 check_word(50272,64'hfedcba9876543210^64'd50266);
 check_word(50276,64'hfedcba9876543210);
 $display("PASS complete JTAG shell full64 first/last token/logit/prompt");$finish;
end endmodule'''
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'tb.sv').write_text(source)
            (root/'core.sv').write_text(toy_core(50257))
            (root/'memory.sv').write_text(emit_memory_sources([])['sv'])
            built=subprocess.run([IVERILOG,'-g2012','-DBSCAN_SIM','-s','tb','-o','simulator',
                'tb.sv','core.sv','memory.sv',
                *(str(ROOT/'rtl'/name) for name in ('graph_onchip_runtime.sv',
                  'graph_tinystories_ypcb_top.sv','tinystories_1m_jtag_csr.sv'))],
                cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(built.returncode,0,built.stderr)
            ran=subprocess.run([VVP,'simulator'],cwd=root,capture_output=True,text=True,timeout=60)
            self.assertEqual(ran.returncode,0,ran.stdout+ran.stderr)
            self.assertIn('PASS complete JTAG shell',ran.stdout)


if __name__ == '__main__':
    unittest.main()
