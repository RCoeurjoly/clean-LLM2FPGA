"""Typed controller join preserves bus signals and explicit readiness gates."""
import inspect,json,os,shutil,subprocess,tempfile,unittest
from pathlib import Path
from test_graph_ddr_bank_adapter import fixture


def controller_fixture():
    support=Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',Path(__file__).resolve().parents[1]))
    contract=json.loads((support/'artifacts/graph-controller-port-contract/export/controller.json').read_text())
    contract['data_width']=64;contract['address_width']=4;contract['physical_data_width']=8
    for p in contract['ports']:
        if p['name']=='user_port_native_0_cmd_addr':p['width']=4
        elif p['name'] in ('user_port_native_0_rdata_data','user_port_native_0_wdata_data'):p['width']=64
        elif p['name']=='user_port_native_0_wdata_we' or p['name']=='ddram_dq':p['width']=8
        elif p['name'] in ('ddram_dqs_p','ddram_dqs_n'):p['width']=1
    return contract

class ControllerAdapterTests(unittest.TestCase):
    def lower(self,controller=None,**kwargs):
        from compiler.ddr_bank_adapter import lower_ddr_bank_adapter
        self.assertIn('controller',inspect.signature(lower_ddr_bank_adapter).parameters,'typed controller join is missing')
        return lower_ddr_bank_adapter(*fixture(),load_images=True,controller=controller or controller_fixture(),**kwargs)
    def test_native_emission_preserves_phy_inouts_and_explicit_calibration_input(self):
        text=self.lower()
        result=subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'],'--verify-each','--export-verilog','-o',os.devnull],input=text,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('closure_litedram',result.stdout)
        self.assertIn('phy_calibrated',result.stdout)
        self.assertIn('ddr_ref_clk',result.stdout)
        self.assertRegex(result.stdout,r'inout\s+\[7:0\]\s+ddram_dq')
    def test_controller_geometry_mismatch_is_rejected(self):
        controller=controller_fixture();controller['data_width']=128
        with self.assertRaisesRegex(ValueError,'controller geometry'):self.lower(controller)
    def test_controller_native_port_direction_mismatch_is_rejected(self):
        controller=controller_fixture()
        next(p for p in controller['ports'] if p['name']=='user_port_native_0_cmd_ready')['direction']='input'
        with self.assertRaisesRegex(ValueError,'controller port'):self.lower(controller)
    def test_controller_requires_loaded_transport(self):
        from compiler.ddr_bank_adapter import lower_ddr_bank_adapter
        self.assertIn('controller',inspect.signature(lower_ddr_bank_adapter).parameters)
        with self.assertRaisesRegex(ValueError,'loaded transport'):
            lower_ddr_bank_adapter(*fixture(),controller=controller_fixture())

    def test_readiness_truth_table_then_full_stream_and_two_client_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);text=self.lower()
            interface,layout=fixture()
            interface['ports'] += [{'name':'value_a','width':64,'direction':'output'},{'name':'value_b','width':64,'direction':'output'}]
            from compiler.ddr_bank_adapter import lower_ddr_bank_adapter
            text=lower_ddr_bank_adapter(interface,layout,load_images=True,controller=controller_fixture())
            emitted=subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'],'--verify-each','--export-verilog','-o',os.devnull],input=text,text=True,capture_output=True,timeout=30)
            self.assertEqual(emitted.returncode,0,emitted.stderr)
            (root/'wrapper.sv').write_text(emitted.stdout)
            # Peers and test values enter only after native wrapper emission.
            write_peers(root,controller_fixture())
            support=Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',Path(__file__).resolve().parents[1]))/'rtl/primitives'
            command=[shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'sim'),str(root/'wrapper.sv'),str(root/'peers.sv'),str(root/'tb.sv')]
            command += [str(support/name) for name in ('closure_ddr_read_bank.sv','closure_ddr_read_port.sv','closure_ddr_read_cdc.sv','closure_ddr_image_loader.sv','closure_ddr_loaded_port.sv')]
            result=subprocess.run(command,capture_output=True,text=True,timeout=30);self.assertEqual(result.returncode,0,result.stderr)
            for core_half,ui_half in [(5,7),(13,3)]:
                run=subprocess.run([shutil.which('vvp'),str(root/'sim'),f'+core_half={core_half}',f'+ui_half={ui_half}'],capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                self.assertIn('PASS readiness=32 writes=9 reads=11',run.stdout)


    def test_emission_stage_authenticates_controller_contract(self):
        from compiler.ddr_bank_adapter import emit_ddr_bank_adapter_stage
        from compiler.pipeline import CompilerTool
        from compiler.stages import verify_stage_receipt
        self.assertIn('controller',inspect.signature(emit_ddr_bank_adapter_stage).parameters,'controller stage dependency is missing')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);interface,layout=fixture()
            for name,document in [('interface.json',interface),('layout.json',layout),('controller.json',controller_fixture())]:
                (root/name).write_text(json.dumps(document))
            emit_ddr_bank_adapter_stage(root/'interface.json',root/'layout.json',root/'adapter',CompilerTool(Path(os.environ['LLM2FPGA_CIRCT_TOOL'])),load_images=True,controller=root/'controller.json')
            deps=verify_stage_receipt(root/'adapter/emission')
            self.assertIn(str(root/'controller.json'),deps)
            spec=json.loads((root/'adapter/emission/spec.json').read_text())
            self.assertTrue(spec['pass_identities']['controller_connected'])
            self.assertIn('phy_calibrated',(root/'adapter/emission/rtl/graph_ddr_bank.sv').read_text())


def write_peers(root,contract):
    ports=['input clk,reset,go','output done','output [63:0]value_a,value_b']
    assigns=['assign done=a_done&b_done;','assign value_a=a_read_data;','assign value_b=b_read_data;']
    for cell,aw,addr in [('a',2,1),('b',3,5)]:
        ports += [f'output[{aw-1}:0]{cell}_addr0',f'output {cell}_clk,{cell}_reset,{cell}_content_en,{cell}_write_en',f'output[63:0]{cell}_write_data',f'input[63:0]{cell}_read_data',f'input {cell}_done']
        assigns += [f"assign {cell}_addr0={aw}'d{addr};",f'assign {cell}_clk=clk;',f'assign {cell}_reset=reset;',f'assign {cell}_content_en=go;',f'assign {cell}_write_en=0;',f'assign {cell}_write_data=0;']
    source='module main('+','.join(ports)+');\n'+'\n'.join(assigns)+'\nendmodule\n'
    registers={'user_port_native_0_rdata_valid','user_port_native_0_rdata_data'}
    ports=[p['direction']+(' reg ' if p['name'] in registers else ' wire ')+(f"[{p['width']-1}:0] " if p['width']>1 else '')+p['name'] for p in contract['ports']]
    driven={'user_clk','user_rst','init_done','init_error','pll_locked','user_port_native_0_cmd_ready','user_port_native_0_wdata_ready',*registers}
    source += 'module closure_litedram('+','.join(ports)+');\n'
    source += '\n'.join('assign '+p['name']+"=0;" for p in contract['ports'] if p['direction']=='output' and p['name'] not in driven)+'\n'
    source += STUB+'\nendmodule\n'
    (root/'peers.sv').write_text(source);(root/'tb.sv').write_text(TB)

STUB="""reg[63:0]memory[0:8];integer ticks=0,writes=0,reads=0;
assign user_clk=clk;assign user_rst=wb_ctrl_dat_w[3];assign init_done=wb_ctrl_dat_w[0];
assign init_error=wb_ctrl_dat_w[1];assign pll_locked=wb_ctrl_dat_w[2];
assign user_port_native_0_cmd_ready=ticks%3!=0&&!user_port_native_0_rdata_valid;
assign user_port_native_0_wdata_ready=ticks%5!=0;
always @(posedge clk)begin
if(rst)begin ticks<=0;writes<=0;reads<=0;user_port_native_0_rdata_valid<=0;user_port_native_0_rdata_data<=0;end
else begin
 ticks<=ticks+1;
 if(user_port_native_0_rdata_valid&&user_port_native_0_rdata_ready)user_port_native_0_rdata_valid<=0;
 if(user_port_native_0_wdata_valid&&user_port_native_0_wdata_ready)begin
  if(user_port_native_0_wdata_we!=8'hff)$fatal(1,"byte enables");memory[user_port_native_0_cmd_addr]<=user_port_native_0_wdata_data;writes<=writes+1;end
 if(user_port_native_0_cmd_valid&&user_port_native_0_cmd_ready&&!user_port_native_0_cmd_we)begin
  user_port_native_0_rdata_valid<=1;user_port_native_0_rdata_data<=memory[user_port_native_0_cmd_addr];reads<=reads+1;end
end end
"""
TB="""module tb;
integer core_half=5,ui_half=7,condition,cycles=0;reg clk=0,ddr_ref_clk=0,reset=1,go=0,phy_calibrated=0,input_valid=0;
reg[31:0]flags=0;wire input_ready,done,fault,core_fault,ui_loaded,ui_load_fault,core_memory_ready;
wire[3:0]frame_index;wire[63:0]value_a,value_b;wire[7:0]ddram_dq;wire ddram_dqs_p,ddram_dqs_n;
wire[63:0]input_data=64'ha000+frame_index;
initial begin if($value$plusargs("core_half=%d",core_half))begin end if($value$plusargs("ui_half=%d",ui_half))begin end end
always #(core_half)clk=~clk;always #(ui_half)ddr_ref_clk=~ddr_ref_clk;
graph_ddr_bank dut(.clk(clk),.reset(reset),.go(go),.ddr_ref_clk(ddr_ref_clk),.phy_calibrated(phy_calibrated),.input_valid(input_valid),.input_ready(input_ready),.input_data(input_data),.frame_index(frame_index),.done(done),.value_a(value_a),.value_b(value_b),.fault(fault),.core_fault(core_fault),.core_memory_ready(core_memory_ready),.ui_loaded(ui_loaded),.ui_load_fault(ui_load_fault),.ddram_dq(ddram_dq),.ddram_dqs_p(ddram_dqs_p),.ddram_dqs_n(ddram_dqs_n),.wb_ctrl_dat_w(flags),.wb_ctrl_adr(30'b0),.wb_ctrl_bte(2'b0),.wb_ctrl_cti(3'b0),.wb_ctrl_cyc(1'b0),.wb_ctrl_sel(4'b0),.wb_ctrl_stb(1'b0),.wb_ctrl_we(1'b0));
always @(posedge clk)begin cycles<=cycles+1;if(cycles>6000)$fatal(1,"timeout");end
initial begin
for(condition=0;condition<32;condition=condition+1)begin
 @(negedge ddr_ref_clk);reset=1;flags=condition%16;phy_calibrated=(condition/16)!=0;
 repeat(4)@(negedge ddr_ref_clk);reset=0;repeat(6)@(negedge ddr_ref_clk);
 if(input_ready !== (flags[0]&&!flags[1]&&flags[2]&&!flags[3]&&phy_calibrated))$fatal(1,"readiness predicate condition=%0d",condition);
 if(dut.controller.writes!=0||dut.controller.reads!=0||core_memory_ready)$fatal(1,"request before verified loading");
end
reset=1;flags=5;phy_calibrated=1;repeat(5)@(negedge ddr_ref_clk);reset=0;input_valid=1;go=1;
wait(done);@(negedge clk);
if(!ui_loaded||ui_load_fault||fault||core_fault||dut.controller.writes!=9||dut.controller.reads!=11||value_a!=64'ha001||value_b!=64'ha008)$fatal(1,"controller join misrouted data");
$display("PASS readiness=32 writes=9 reads=11");$finish;
end endmodule
"""

