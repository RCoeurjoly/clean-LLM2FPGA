"""Native HW lowering for board memory joins; never replace generated RTL."""
import importlib,json,os,subprocess,tempfile,unittest
from pathlib import Path

def fixture():
    ports=[{'name':n,'width':1,'direction':d} for n,d in [('clk','input'),('reset','input'),('go','input'),('done','output')]]
    memories=[]
    for cell,depth,address in [('a',3,2),('b',6,3)]:
        memory={'component':'main','cell':cell,'implementation':'ddr3','width':64,'depth':depth,'address_width':address,'read_latency':0,'writable':False,'ports':[]}
        for role,width,direction in [('addr0',address,'output'),('clk',1,'output'),('reset',1,'output'),('content_en',1,'output'),('write_en',1,'output'),('write_data',64,'output'),('read_data',64,'input'),('done',1,'input')]:
            memory['ports'].append({'name':cell+'_'+role,'role':role,'width':width,'direction':direction})
        memories.append(memory)
    interface={'schema_version':1,'entrypoint':'main','ports':ports,'memories':memories}
    layout={'schema_version':1,'data_width':64,'address_width':4,'slot_width':64,'words_per_frame':1,'frames':9,'memories':[{'component':'main','cell':'b','width':64,'depth':6,'base_frame':3,'frame_count':6},{'component':'main','cell':'a','width':64,'depth':3,'base_frame':0,'frame_count':3}]}
    return interface,layout

class DDRBankAdapterTests(unittest.TestCase):
    def lower(self,interface,layout):
        name='compiler.ddr_bank_adapter'
        self.assertIsNotNone(importlib.util.find_spec(name),'typed HW bank adapter lowering is missing')
        return importlib.import_module(name).lower_ddr_bank_adapter(interface,layout)
    def test_native_hw_verifier_and_verilog_emitter_accept_join(self):
        text=self.lower(*fixture())
        result=subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'],'--verify-each','--export-verilog','-o',os.devnull],input=text,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('module graph_ddr_bank',result.stdout)
        self.assertIn('closure_ddr_read_bank',result.stdout)
        self.assertRegex(result.stdout,r'\.CLIENTS\s*\(2\)')
    def test_layout_depth_mismatch_is_rejected(self):
        interface,layout=fixture();layout['memories'][0]['depth']=5
        with self.assertRaisesRegex(ValueError,'geometry'):self.lower(interface,layout)
    def test_missing_client_is_rejected(self):
        interface,layout=fixture();layout['memories'].pop()
        with self.assertRaisesRegex(ValueError,'client set'):self.lower(interface,layout)
    def test_writable_ddr_is_rejected(self):
        interface,layout=fixture();interface['memories'][0]['writable']=True
        with self.assertRaisesRegex(ValueError,'immutable'):self.lower(interface,layout)

    def test_native_join_routes_reversed_layout_and_zero_extended_addresses(self):
        import shutil
        interface,layout=fixture()
        interface['ports'] += [{'name':'value_a','width':64,'direction':'output'}, {'name':'value_b','width':64,'direction':'output'}]
        text=self.lower(interface,layout)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            emit=subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'],'--verify-each','--export-verilog','-o',os.devnull],input=text,text=True,capture_output=True,timeout=30)
            self.assertEqual(emit.returncode,0,emit.stderr)
            (root/'wrapper.sv').write_text(emit.stdout)
            # Authored core fixture and peer are supplied only after emission.
            ports=['input clk,reset,go','output done','output [63:0] value_a,value_b']
            assigns=['assign done=a_done & b_done;','assign value_a=a_read_data;','assign value_b=b_read_data;']
            for cell,aw,addr in [('a',2,1),('b',3,5)]:
                ports += [f'output [{aw-1}:0] {cell}_addr0',f'output {cell}_clk,{cell}_reset,{cell}_content_en,{cell}_write_en',f'output [63:0] {cell}_write_data',f'input [63:0] {cell}_read_data',f'input {cell}_done']
                assigns += [f'assign {cell}_addr0={aw}\'d{addr};',f'assign {cell}_clk=clk;',f'assign {cell}_reset=reset;',f'assign {cell}_content_en=go;',f'assign {cell}_write_en=0;',f'assign {cell}_write_data=0;']
            (root/'core.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(assigns)+'\nendmodule\n')
            (root/'tb.sv').write_text("""module tb;
reg clk=0,reset=1,go=0;always #5 clk=~clk;
wire done,fault,cmd_valid,cmd_we,rdata_ready;wire[3:0]cmd_addr;
wire[63:0]value_a,value_b;reg rdata_valid=0;reg[63:0]rdata=0;
integer requests=0,cycles=0;
graph_ddr_bank dut(.clk(clk),.reset(reset),.go(go),.done(done),.value_a(value_a),.value_b(value_b),.memory_ready(1'b1),.fault(fault),.cmd_valid(cmd_valid),.cmd_ready(1'b1),.cmd_we(cmd_we),.cmd_addr(cmd_addr),.rdata_valid(rdata_valid),.rdata_ready(rdata_ready),.rdata(rdata));
always @(posedge clk) begin
 if(reset)begin rdata_valid<=0;requests<=0;end
 else begin
  if(rdata_valid&&rdata_ready)rdata_valid<=0;
  if(cmd_valid)begin if(cmd_we)$fatal;requests<=requests+1;rdata<=64'ha000+cmd_addr;rdata_valid<=1;end
  if(fault)$fatal(1,"bank fault");
  cycles<=cycles+1;if(cycles>200)$fatal(1,"timeout");
 end
end
initial begin repeat(3)@(negedge clk);reset=0;go=1;wait(done);@(negedge clk);
if(requests!=2||value_a!=64'ha001||value_b!=64'ha008)$fatal(1,"misrouted reads");
$display("PASS requests=%0d a=%h b=%h",requests,value_a,value_b);$finish;end
endmodule
""")
            support=Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',Path(__file__).resolve().parents[1]))/'rtl/primitives'
            compile=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'sim'),str(root/'wrapper.sv'),str(root/'core.sv'),str(root/'tb.sv'),str(support/'closure_ddr_read_bank.sv'),str(support/'closure_ddr_read_port.sv')],capture_output=True,text=True,timeout=30)
            self.assertEqual(compile.returncode,0,compile.stderr)
            run=subprocess.run([shutil.which('vvp'),str(root/'sim')],capture_output=True,text=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn('PASS requests=2',run.stdout)

    def test_native_join_preserves_all_runtime_memory_ports(self):
        """Runtime words and completion remain external to the DDR bank."""
        import shutil
        interface, layout = fixture()
        runtime = [('prompt', 4, 2), ('generated_tokens', 16, 4),
                   ('last_logits', 50257, 16)]
        for cell, depth, aw in runtime:
            memory = dict(component='main', cell=cell, implementation='bram',
                          width=64, depth=depth, address_width=aw,
                          read_latency=0, writable=True, ports=[])
            for role, bits, direction in (
                    ('addr0', aw, 'output'), ('clk', 1, 'output'),
                    ('reset', 1, 'output'), ('content_en', 1, 'output'),
                    ('write_en', 1, 'output'), ('write_data', 64, 'output'),
                    ('read_data', 64, 'input'), ('done', 1, 'input')):
                memory['ports'].append(dict(name=cell+'_'+role, role=role,
                                            width=bits, direction=direction))
            interface['memories'].append(memory)
        text = self.lower(interface, layout)
        emit = subprocess.run([os.environ['LLM2FPGA_CIRCT_TOOL'], '--verify-each',
                               '--export-verilog', '-o', os.devnull], input=text,
                              text=True, capture_output=True, timeout=30)
        self.assertEqual(emit.returncode, 0, emit.stderr)
        ports = ['input clk,reset,go', 'output done']
        assignments = ['assign done=prompt_done & generated_tokens_done & last_logits_done;']
        for memory in interface['memories']:
            cell = memory['cell']
            for port in memory['ports']:
                # The fixture core has the same declared native interface.
                bits = '' if port['width'] == 1 else f"[{port['width']-1}:0] "
                ports.append(port['direction']+' '+bits+port['name'])
            assignments += [f'assign {cell}_clk=clk;', f'assign {cell}_reset=reset;',
                            f'assign {cell}_content_en=go;',
                            f"assign {cell}_addr0={memory['address_width']}'d{memory['depth']-1};",
                            f"assign {cell}_write_en={'go' if memory['writable'] else '0'};",
                            f"assign {cell}_write_data=" +
                            ('prompt_read_data ^ 64\'hfedcba9876543210;' if memory['writable'] else '0;')]
        connections = ['.clk(clk)', '.reset(reset)', '.go(go)', '.done(done)',
                       '.memory_ready(1\'b0)', '.cmd_ready(1\'b0)',
                       '.rdata_valid(1\'b0)', '.rdata(64\'b0)']
        declarations = []
        checks = []
        for cell, depth, aw in runtime:
            for role, bits in [('addr0', aw), ('clk', 1), ('reset', 1),
                               ('content_en', 1), ('write_en', 1), ('write_data', 64)]:
                declarations.append('wire '+('' if bits == 1 else f'[{bits-1}:0] ')+cell+'_'+role+';')
                connections.append('.'+cell+'_'+role+'('+cell+'_'+role+')')
            connections += [f'.{cell}_read_data(data)', f'.{cell}_done(completion)']
            checks.append(f"if({cell}_addr0!={depth-1} || {cell}_clk!==clk || "
                          f"{cell}_reset!==reset || {cell}_content_en!==go || "
                          f"{cell}_write_en!==go || {cell}_write_data!==(data ^ 64'hfedcba9876543210)) "
                          f'$fatal(1,"runtime passthrough {cell}");')
        tb = ('module tb; reg clk=0,reset=1,go=0,completion=0; reg[63:0]data=0; '
              'wire done; integer i; always #5 clk=~clk; '+''.join(declarations)+
              'graph_ddr_bank dut('+','.join(connections)+'); initial begin '
              'for(i=0;i<64;i=i+1)begin @(negedge clk); reset=(i==0);go=i%2;'
              "completion=(i%3==0);data=(64'h8000000000000001<<i)^64'h0123456789abcdef;#1;"+
              ''.join(checks)+'if(done!==completion)$fatal(1,"runtime completion");end '
              '$display("PASS runtime ports all64 bits");$finish;end endmodule')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'wrapper.sv').write_text(emit.stdout)
            (root/'core.sv').write_text('module main('+','.join(ports)+');'+
                                      ''.join(assignments)+'endmodule')
            (root/'tb.sv').write_text(tb)
            support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT', Path(__file__).resolve().parents[1]))/'rtl/primitives'
            result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s', 'tb',
                '-o', str(root/'sim'), str(root/'wrapper.sv'), str(root/'core.sv'),
                str(root/'tb.sv'), str(support/'closure_ddr_read_bank.sv'),
                str(support/'closure_ddr_read_port.sv')], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([shutil.which('vvp'), str(root/'sim')],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('PASS runtime ports all64 bits', result.stdout)

    def test_emission_receipt_authenticates_typed_contract_and_native_output(self):
        from compiler.pipeline import CompilerTool
        from compiler.stages import verify_stage_receipt
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);interface,layout=fixture()
            for name,document in [('interface.json',interface),('layout.json',layout)]:
                (root/name).write_text(json.dumps(document))
            module=importlib.import_module('compiler.ddr_bank_adapter')
            self.assertTrue(hasattr(module,'emit_ddr_bank_adapter_stage'),'authenticated emission stage is missing')
            result=module.emit_ddr_bank_adapter_stage(root/'interface.json',root/'layout.json',root/'adapter',CompilerTool(Path(os.environ['LLM2FPGA_CIRCT_TOOL'])))
            self.assertEqual(result.exit_code,0)
            dependencies=verify_stage_receipt(root/'adapter/emission')
            self.assertIn(str(root/'interface.json'),dependencies)
            self.assertIn(str(root/'layout.json'),dependencies)
            self.assertIn(str(root/'adapter/emission/rtl/graph_ddr_bank.sv'),dependencies)
