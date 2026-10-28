"""Execute native HW-generated bank/loader/CDC joins with independent peers."""
import json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
from test_graph_ddr_bank_adapter import fixture

class LoadedAdapterTests(unittest.TestCase):
    def emit(self,root):
        from compiler.ddr_bank_adapter import lower_ddr_bank_adapter
        interface,layout=fixture()
        interface['ports'] += [{'name':'value_a','width':64,'direction':'output'}, {'name':'value_b','width':64,'direction':'output'}]
        import inspect
        self.assertIn('load_images',inspect.signature(lower_ddr_bank_adapter).parameters,'loaded HW wiring is missing')
        text=lower_ddr_bank_adapter(interface,layout,load_images=True)
        result=subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'],'--verify-each','--export-verilog','-o',os.devnull],input=text,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        (root/'wrapper.sv').write_text(result.stdout)
        return result.stdout
    def test_native_emission_contains_loader_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            text=self.emit(Path(temporary))
            self.assertIn('closure_ddr_loaded_port',text)
            self.assertIn('ui_clk',text)
            self.assertIn('ui_load_fault',text)
    def test_emission_stage_records_loading_policy_and_native_outputs(self):
        import inspect
        from compiler.ddr_bank_adapter import emit_ddr_bank_adapter_stage
        from compiler.pipeline import CompilerTool
        from compiler.stages import verify_stage_receipt
        self.assertIn('load_images',inspect.signature(emit_ddr_bank_adapter_stage).parameters,'loading policy is absent from stage')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);interface,layout=fixture()
            for name,document in [('interface.json',interface),('layout.json',layout)]:
                (root/name).write_text(json.dumps(document))
            emit_ddr_bank_adapter_stage(root/'interface.json',root/'layout.json',root/'adapter',CompilerTool(Path(os.environ['LLM2FPGA_CIRCT_TOOL'])),load_images=True)
            verify_stage_receipt(root/'adapter/emission')
            spec=json.loads((root/'adapter/emission/spec.json').read_text())
            self.assertTrue(spec['pass_identities']['load_images'])
            self.assertIn('closure_ddr_loaded_port',(root/'adapter/emission/rtl/graph_ddr_bank.sv').read_text())

    def simulate(self,corrupt):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.emit(root)
            # Test core and controller peer are constructed after compilation.
            ports=['input clk,reset,go','output done','output [63:0] value_a,value_b']
            assignments=['assign done=a_done & b_done;','assign value_a=a_read_data;','assign value_b=b_read_data;']
            for cell,aw,addr in [('a',2,1),('b',3,5)]:
                ports += [f'output [{aw-1}:0] {cell}_addr0',f'output {cell}_clk,{cell}_reset,{cell}_content_en,{cell}_write_en',f'output [63:0] {cell}_write_data',f'input [63:0] {cell}_read_data',f'input {cell}_done']
                assignments += [f"assign {cell}_addr0={aw}'d{addr};",f'assign {cell}_clk=clk;',f'assign {cell}_reset=reset;',f'assign {cell}_content_en=go;',f'assign {cell}_write_en=0;',f'assign {cell}_write_data=0;']
            (root/'core.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(assignments)+'\nendmodule\n')
            (root/'tb.sv').write_text(TB)
            support=Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',Path(__file__).resolve().parents[1]))/'rtl/primitives'
            command=[shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'sim'),str(root/'wrapper.sv'),str(root/'core.sv'),str(root/'tb.sv')]
            command += [str(support/name) for name in ('closure_ddr_read_bank.sv','closure_ddr_read_port.sv','closure_ddr_read_cdc.sv','closure_ddr_image_loader.sv','closure_ddr_loaded_port.sv')]
            compile=subprocess.run(command,capture_output=True,text=True,timeout=30);self.assertEqual(compile.returncode,0,compile.stderr)
            for core_half,ui_half in [(5,7),(13,3)]:
                run=subprocess.run([shutil.which('vvp'),str(root/'sim'),f'+core_half={core_half}',f'+ui_half={ui_half}',f'+corrupt={int(corrupt)}'],capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                self.assertIn('PASS',run.stdout)
    def test_streamed_frames_are_verified_before_two_client_inference(self):self.simulate(False)
    def test_corrupted_readback_never_activates_core_memory(self):self.simulate(True)

TB='''module tb;
integer core_half=5,ui_half=7,corrupt=0;reg clk=0,ui_clk=0,reset=1,go=0;
initial begin if($value$plusargs("core_half=%d",core_half))begin end if($value$plusargs("ui_half=%d",ui_half))begin end if($value$plusargs("corrupt=%d",corrupt))begin end end
always #(core_half)clk=~clk;always #(ui_half)ui_clk=~ui_clk;
wire done,fault,core_fault,core_memory_ready,ui_loaded,ui_load_fault;
wire cmd_valid,cmd_we,wdata_valid,rdata_ready,input_ready;wire[3:0]cmd_addr,frame_index;
wire[63:0]value_a,value_b,wdata;wire[7:0]wdata_we;
reg rdata_valid=0;reg[63:0]rdata=0;reg[63:0]memory[0:8];
integer ticks=0,writes=0,reads=0,cycles=0;
wire cmd_ready=ticks%3!=0&&!rdata_valid;wire wdata_ready=ticks%5!=0;
wire[63:0]input_data=64'ha000+frame_index;
graph_ddr_bank dut(.clk(clk),.ui_clk(ui_clk),.reset(reset),.go(go),.done(done),.value_a(value_a),.value_b(value_b),.fault(fault),.controller_ready(1'b1),.input_valid(1'b1),.input_ready(input_ready),.input_data(input_data),.frame_index(frame_index),.ui_loaded(ui_loaded),.ui_load_fault(ui_load_fault),.core_fault(core_fault),.core_memory_ready(core_memory_ready),.cmd_valid(cmd_valid),.cmd_ready(cmd_ready),.cmd_we(cmd_we),.cmd_addr(cmd_addr),.wdata_valid(wdata_valid),.wdata_ready(wdata_ready),.wdata(wdata),.wdata_we(wdata_we),.rdata_valid(rdata_valid),.rdata_ready(rdata_ready),.rdata(rdata));
always @(posedge ui_clk)begin
 if(reset)begin ticks<=0;writes<=0;reads<=0;rdata_valid<=0;end
 else begin
  ticks<=ticks+1;
  if(rdata_valid&&rdata_ready)rdata_valid<=0;
  if(wdata_valid&&wdata_ready)begin if(wdata_we!=8'hff)$fatal(1,"byte enables");memory[cmd_addr]<=wdata;writes<=writes+1;end
  if(cmd_valid&&cmd_ready&&!cmd_we)begin rdata_valid<=1;rdata<=memory[cmd_addr]^(corrupt?64'h8000000000000000:64'h0);reads<=reads+1;end
 end
end
always @(posedge clk)begin
 if(!reset)begin cycles<=cycles+1;if(fault||core_fault)$fatal(1,"core transport fault");if(cycles>2000)$fatal(1,"timeout");end
end
initial begin repeat(8)@(negedge clk);reset=0;go=1;
if(corrupt)begin wait(ui_load_fault);repeat(20)@(negedge clk);if(done||ui_loaded||core_memory_ready||reads!=1)$fatal(1,"corrupt image activated inference");$display("PASS corruption blocked");end
else begin wait(done);@(negedge clk);if(!ui_loaded||ui_load_fault||writes!=9||reads!=11||value_a!=64'ha001||value_b!=64'ha008)$fatal(1,"incorrect composed inference");$display("PASS writes=%0d reads=%0d",writes,reads);end
$finish;end
endmodule
'''
