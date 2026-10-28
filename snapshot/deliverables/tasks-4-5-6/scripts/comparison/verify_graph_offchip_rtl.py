"""Qualification-only authored IR fixture for the off-chip request contract.

No model schedule or expected result enters production compilation. The peer
loads the same authenticated input image and checks request stability, delayed
responses and retirement. This is not graph or whole-model equivalence proof.
"""
import json
from pathlib import Path
import re
from compiler.pipeline import CALYX_RESOURCE_PASSES
from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt, _write_json
from compiler.ddr_images import materialize_ddr_images
from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources

SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}, %value: i8) {
    %false = hw.constant false
    %true = hw.constant true
    %a0 = hw.constant 0 : i2
    %a2 = hw.constant 2 : i2
    %zero = hw.constant 0 : i8
    %last.in, %last.write_en, %last.clk, %last.reset, %last.out, %last.done = calyx.register @last : i8, i1, i1, i1, i8, i1
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.seq_mem @m <[3] x 8> [2] {external = true} : i2, i1, i1, i1, i1, i8, i8, i1
    calyx.wires {
      calyx.assign %value = %last.out : i8
      calyx.group @read0 {
        calyx.assign %m.addr0 = %a0 : i2
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = %false : i1
        calyx.assign %m.write_data = %zero : i8
        calyx.group_done %m.done : i1
      }
      calyx.group @read2 {
        calyx.assign %m.addr0 = %a2 : i2
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = %false : i1
        calyx.assign %m.write_data = %zero : i8
        calyx.group_done %m.done : i1
      }
      calyx.group @record {
        calyx.assign %last.in = %m.read_data : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control { calyx.seq { calyx.enable @read0 calyx.enable @record calyx.enable @read2 calyx.enable @record } }
  } {toplevel}
}
'''

# Additional authored IR fixture: no unrelated group separates the reads.
BACK_TO_BACK_SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}, %value: i8) {
    %false = hw.constant false
    %true = hw.constant true
    %a0 = hw.constant 0 : i2
    %a2 = hw.constant 2 : i2
    %zero = hw.constant 0 : i8
    %last.in, %last.write_en, %last.clk, %last.reset, %last.out, %last.done = calyx.register @last : i8, i1, i1, i1, i8, i1
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.seq_mem @m <[3] x 8> [2] {external = true} : i2, i1, i1, i1, i1, i8, i8, i1
    calyx.wires {
      calyx.assign %value = %last.out : i8
      calyx.group @read0 {
        calyx.assign %m.addr0 = %a0 : i2
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = %false : i1
        calyx.assign %m.write_data = %zero : i8
        calyx.group_done %m.done : i1
      }
      calyx.group @read2 {
        calyx.assign %m.addr0 = %a2 : i2
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = %false : i1
        calyx.assign %m.write_data = %zero : i8
        calyx.group_done %m.done : i1
      }
      calyx.group @record {
        calyx.assign %last.in = %m.read_data : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control { calyx.seq { calyx.enable @read0 calyx.enable @read2 calyx.enable @record } }
  } {toplevel}
}
'''

TESTBENCH = '''module tb;
 reg clk=0; always #5 clk=~clk;
 reg reset=1,go=0; wire done; wire [7:0] value;
 wire [1:0] addr; wire en,we,mc,mr; wire [7:0] wd;
 reg [7:0] rd=0,words[0:2]; reg md=0;
 integer state=0,count=0,requests=0,delay_cycles=1,cycles=0,report;
 reg [1:0] accepted_addr=0;
 main dut(.clk(clk),.reset(reset),.go(go),.done(done),.value(value),
 .m_addr0(addr),.m_clk(mc),.m_reset(mr),.m_content_en(en),
 .m_write_en(we),.m_write_data(wd),.m_read_data(rd),.m_done(md));
 initial begin
  $readmemh("weights.hex",words);
  if (!$value$plusargs("DELAY=%d",delay_cycles)) delay_cycles=1;
  repeat(4) @(negedge clk); reset=0;go=1;
  wait(done); @(negedge clk);
  if(requests!=2 || value!==words[2]) $fatal(1,"incorrect delayed read result requests=%0d value=%02h expected=%02h",requests,value,words[2]);
  report=$fopen("result.txt","w");
  $fdisplay(report,"RESULT %0d %02h %0d",requests,value,cycles);$fclose(report);
  $display("RESULT %0d %02h %0d",requests,value,cycles); $finish;
 end
 always @(posedge clk) begin
  cycles<=cycles+1;
  if(cycles>2000) $fatal(1,"caller did not complete");
 end
 always @(posedge mc) begin
  if(mr) begin state<=0;md<=0;rd<=0;requests<=0; end
  else case(state)
   0: if(en) begin
    if($isunknown({addr,we}) || we || addr>2) $fatal(1,"invalid immutable request");
    accepted_addr<=addr;requests<=requests+1;
    count<=delay_cycles+requests*3;state<=1;
   end
   1: begin
    if(!en || we || addr!==accepted_addr) $fatal(1,"request changed before completion");
    if(count==0) begin rd<=words[accepted_addr];md<=1;state<=2;end
    else count<=count-1;
   end
   2: if(!en) begin count<=delay_cycles+7;state<=3;end
   3: begin
    if(en) $fatal(1,"new request before previous completion retired");
    if(count==0) begin md<=0;state<=0;end else count<=count-1;
   end
  endcase
 end
endmodule
'''


BANK_CDC_TESTBENCH = '''module tb;
 integer core_half=17,ui_half=6,phase=1,delay_cycles=1;
 reg clk=0,ui_clk=0,reset=1,go=0;wire done;wire[7:0]value;
 integer invalid_write=0;
 initial begin if(!$value$plusargs("CORE=%d",core_half))core_half=17;forever #(core_half)clk=~clk;end
 initial begin
  if(!$value$plusargs("UI=%d",ui_half))ui_half=6;
  if(!$value$plusargs("PHASE=%d",phase))phase=1;
  #(phase);forever #(ui_half)ui_clk=~ui_clk;
 end
 wire [1:0]addr;wire en,we,mc,mr;wire[7:0]wd;
 wire[63:0]bank_data;wire md,bank_fault,cdc_fault,memory_ready;
 wire bcv,bcr,bcw,brr,brv;wire[24:0]bca;wire[575:0]brd;
 wire ncv,ncw,nrr;wire[24:0]nca;
 reg ncr=0,nrv=0;reg[575:0]nrd=0;
 reg[7:0]words[0:2];integer state=0,count=0,requests=0,cycles=0,report,lane;
 reg[24:0]accepted=0;
 main dut(.clk(clk),.reset(reset),.go(go),.done(done),.value(value),
 .m_addr0(addr),.m_clk(mc),.m_reset(mr),.m_content_en(en),
 .m_write_en(we),.m_write_data(wd),.m_read_data(bank_data[7:0]),.m_done(md));
 closure_ddr_read_bank #(.DEPTHS(32'd3)) bank(
 .clk(mc),.reset(mr),.memory_ready(memory_ready),.content_en(en),.write_en(we || invalid_write!=0),
 .addr({30'd0,addr}),.read_data(bank_data),.done(md),.fault(bank_fault),
 .cmd_valid(bcv),.cmd_ready(bcr),.cmd_we(bcw),.cmd_addr(bca),
 .rdata_valid(brv),.rdata_ready(brr),.rdata(brd));
 closure_ddr_read_cdc bridge(.core_clk(mc),.ui_clk(ui_clk),.reset(mr),
 .ui_memory_ready(1'b1),.core_memory_ready(memory_ready),.fault(cdc_fault),
 .core_cmd_valid(bcv),.core_cmd_ready(bcr),.core_cmd_we(bcw),.core_cmd_addr(bca),
 .core_rdata_valid(brv),.core_rdata_ready(brr),.core_rdata(brd),
 .ui_cmd_valid(ncv),.ui_cmd_ready(ncr),.ui_cmd_we(ncw),.ui_cmd_addr(nca),
 .ui_rdata_valid(nrv),.ui_rdata_ready(nrr),.ui_rdata(nrd));
 initial begin
  $readmemh("weights.hex",words);
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=1;
  if(!$value$plusargs("INVALID_WRITE=%d",invalid_write))invalid_write=0;
  repeat(7)@(negedge clk);reset=0;go=1;
  wait(done);@(negedge clk);
  if(requests!=2||value!==words[2])$fatal(1,"composed read mismatch");
  report=$fopen("result.txt","w");
  $fdisplay(report,"RESULT %0d %02h %0d",requests,value,cycles);$fclose(report);
  $display("RESULT %0d %02h %0d",requests,value,cycles);$finish;
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>2000)$fatal(1,"composed caller timeout");
  if(!reset&&(bank_fault||cdc_fault))$fatal(1,"DDR transport fault");
 end
 always @(posedge ui_clk)begin
  if(mr)begin state<=0;ncr<=0;nrv<=0;requests<=0;end else case(state)
   0:if(ncv)begin
    if(ncw||nca!==25'd0)$fatal(1,"invalid native image command");
    accepted<=nca;count<=3;state<=1;
   end
   1:begin
    if(!ncv||nca!==accepted)$fatal(1,"native command changed under backpressure");
    if(count==0)begin ncr<=1;state<=2;end else count<=count-1;
   end
   2:begin
    if(!ncv||nca!==accepted)$fatal(1,"native command vanished");
    ncr<=0;requests<=requests+1;count<=delay_cycles+requests*3;state<=3;
   end
   3:if(count==0)begin
    nrd<=0;for(lane=0;lane<3;lane=lane+1)nrd[lane*64+:64]<={56'd0,words[lane]};
    nrv<=1;state<=4;
   end else count<=count-1;
   4:if(nrr)begin nrv<=0;state<=0;end
  endcase
 end
endmodule
'''


BANK_LOADED_CDC_TESTBENCH = '''module tb;
 integer core_half=17,ui_half=6,phase=1,delay_cycles=1,order=0,corrupt_load=0;
 reg clk=0,ui_clk=0,reset=1,go=0;wire done;wire[7:0]value;
 initial begin if(!$value$plusargs("CORE=%d",core_half))core_half=17;forever #(core_half)clk=~clk;end
 initial begin
  if(!$value$plusargs("UI=%d",ui_half))ui_half=6;
  if(!$value$plusargs("PHASE=%d",phase))phase=1;
  #(phase);forever #(ui_half)ui_clk=~ui_clk;
 end
 wire[1:0]addr;wire en,we,mc,mr;wire[7:0]wd;
 wire[63:0]bank_data;wire md,bank_fault,cdc_fault,memory_ready,loaded,load_fault;
 wire bcv,bcr,bcw,brr,brv;wire[24:0]bca,index;wire[575:0]brd;
 wire ncv,ncw,nrr,nwv,ir;wire[24:0]nca;wire[575:0]nwd;wire[71:0]nwe;
 reg ready=0,iv=0,ncr=0,nwr=0,nrv=0;reg[575:0]nrd=0;
 reg[575:0]input_frame[0:0],stored_frame=0,held_write=0;
 reg command_pending=0,data_pending=0,read_pending=0;
 integer count=0,requests=0,writes=0,load_reads=0,cycles=0,report;
 main dut(.clk(clk),.reset(reset),.go(go),.done(done),.value(value),
 .m_addr0(addr),.m_clk(mc),.m_reset(mr),.m_content_en(en),
 .m_write_en(we),.m_write_data(wd),.m_read_data(bank_data[7:0]),.m_done(md));
 closure_ddr_read_bank #(.DEPTHS(32'd3)) bank(
 .clk(mc),.reset(mr),.memory_ready(memory_ready),.content_en(en),.write_en(we),
 .addr({30'd0,addr}),.read_data(bank_data),.done(md),.fault(bank_fault),
 .cmd_valid(bcv),.cmd_ready(bcr),.cmd_we(bcw),.cmd_addr(bca),
 .rdata_valid(brv),.rdata_ready(brr),.rdata(brd));
 closure_ddr_loaded_port transport(.core_clk(mc),.ui_clk(ui_clk),.reset(mr),
 .controller_ready(ready),.input_valid(iv),.input_ready(ir),.input_data(input_frame[0]),
 .frame_index(index),.ui_loaded(loaded),.ui_load_fault(load_fault),
 .core_memory_ready(memory_ready),.core_fault(cdc_fault),
 .core_cmd_valid(bcv),.core_cmd_ready(bcr),.core_cmd_we(bcw),.core_cmd_addr(bca),
 .core_rdata_valid(brv),.core_rdata_ready(brr),.core_rdata(brd),
 .cmd_valid(ncv),.cmd_ready(ncr),.cmd_we(ncw),.cmd_addr(nca),
 .wdata_valid(nwv),.wdata_ready(nwr),.wdata(nwd),.wdata_we(nwe),
 .rdata_valid(nrv),.rdata_ready(nrr),.rdata(nrd));
 initial begin
  $readmemh("frames.hex",input_frame);
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=1;
  if(!$value$plusargs("ORDER=%d",order))order=0;
  if(!$value$plusargs("CORRUPT_LOAD=%d",corrupt_load))corrupt_load=0;
  repeat(7)@(negedge clk);reset=0;go=1;
  repeat(7)@(negedge ui_clk);ready=1;iv=1;
  do @(posedge ui_clk);while(!ir);
  @(negedge ui_clk);iv=0;
  wait(done);@(negedge clk);
  if(!loaded||writes!=1||load_reads!=1||requests!=2||value!==input_frame[0][128+:8])
   $fatal(1,"loaded caller mismatch");
  report=$fopen("result.txt","w");
  $fdisplay(report,"RESULT %0d %02h %0d",requests,value,cycles);$fclose(report);
  $display("RESULT %0d %02h %0d",requests,value,cycles);$finish;
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>4000)$fatal(1,"loaded caller timeout");
  if(!reset&&(bank_fault||cdc_fault))$fatal(1,"DDR transport fault");
  if(!reset&&!loaded&&(memory_ready||md||done))$fatal(1,"caller completed before verified loading");
 end
 always @(negedge ui_clk)begin
  ncr=ready&&!mr&&!command_pending&&!read_pending&&(!ncw||order==0||data_pending);
  nwr=ready&&!mr&&!data_pending&&(order==1||command_pending);
 end
 always @(posedge ui_clk)begin
  if(mr)begin
   command_pending<=0;data_pending<=0;read_pending<=0;nrv<=0;
   requests<=0;load_reads<=0;writes<=0;stored_frame<=0;
  end else begin
   if(load_fault)$fatal(1,"DDR load readback fault");
   if(ncv&&ncr)begin
    if(nca!==25'd0)$fatal(1,"invalid image address");
    if(ncw)begin
     if(loaded)$fatal(1,"write after loading");command_pending<=1;writes<=writes+1;
    end else begin
     read_pending<=1;count<=delay_cycles;
     if(loaded)requests<=requests+1;else load_reads<=load_reads+1;
    end
   end
   if(nwv&&nwr)begin
    if(nwe!=={72{1'b1}})$fatal(1,"partial image write");
    data_pending<=1;held_write<=nwd;
   end
   if(command_pending&&data_pending)begin
    stored_frame<=held_write;command_pending<=0;data_pending<=0;
   end
   if(read_pending&&!command_pending&&!data_pending&&!nrv)begin
    if(count==0)begin
     nrd<=corrupt_load!=0&&!loaded ? stored_frame^(576'd1<<575):stored_frame;nrv<=1;
    end else count<=count-1;
   end
   if(nrv)begin if(!nrr)$fatal(1,"native pulse missed");nrv<=0;read_pending<=0;end
  end
 end
endmodule
'''


def qualify_offchip_caller(root, tools, *, retirement=True, transport="peer", invalid_write=False,
                           corrupt_load=False):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    (root/'qualification.json').unlink(missing_ok=True)
    if transport not in ("peer", "bank-cdc", "bank-loaded-cdc"):
        raise ValueError("unsupported qualification transport")
    if type(invalid_write) is not bool or (invalid_write and transport != "bank-cdc"):
        raise ValueError("invalid qualification write injection")
    if type(corrupt_load) is not bool or (corrupt_load and transport != 'bank-loaded-cdc'):
        raise ValueError('invalid qualification image corruption injection')
    scope = ("Native typed memory-control fixture only" if transport == "peer" else
             "Native typed caller with shared bank and CDC; digital boundary only")
    if transport=='bank-loaded-cdc':
        scope='Native typed caller with verified image loading, shared bank and CDC; digital boundary only'
    source=root/'caller.mlir';source.write_text(SOURCE if transport == 'peer' else BACK_TO_BACK_SOURCE)
    image=root/'weights.hex';image.write_bytes(b'a7\n00\nd1\n')
    manifest=root/'bindings.json'
    _write_json(manifest,{'schema_version':1,'bindings':[dict(component='main',cell='m',
        width=8,depth=3,address_width=2,implementation='ddr3',read_latency=0,
        writable=False,init_file=image.name,init_sha256=digest(image))]})
    primitive=root/'closure_memory.sv'
    primitive.write_text(emit_memory_sources([MemorySpec('fixture',8,3,'bram',2,False,image.name)])['sv'])
    tb=root/'tb.sv';tb.write_text({'peer':TESTBENCH,'bank-cdc':BANK_CDC_TESTBENCH,
                                 'bank-loaded-cdc':BANK_LOADED_CDC_TESTBENCH}[transport])
    dependencies={str(p):digest(p) for p in (source,image,manifest,primitive,Path(__file__).resolve())}
    tools={name:Path(path).resolve(strict=True) for name,path in tools.items()}
    library=tools['calyx'].parent.parent/'share/calyx'
    stages=[]
    def stage(name,argv,outputs):
        nonlocal dependencies
        identities={str(p):digest(p) for p in tools.values()}
        run_stage(StageSpec(name,tuple(str(a) for a in argv),dependencies,identities,
            {'scope':scope,'retirement':retirement,'transport':transport,'invalid_write':invalid_write,
             'corrupt_load':corrupt_load},tuple(outputs),60),root/name)
        dependencies={**dependencies,**verify_stage_receipt(root/name)}
        stages.append(name)
    arguments=[tools['memory'],source,'--verify-each',
        '--llm2fpga-validate-memory-bindings=manifest='+str(manifest)+' manifest-sha256='+digest(manifest)]
    if retirement:arguments.append('--llm2fpga-retire-memory-requests')
    arguments += ['--llm2fpga-bind-model-memories=primitive-source='+str(primitive)+' primitive-source-sha256='+digest(primitive),
                  '--emit-bytecode','-o','caller.mlirbc']
    stage('binding',arguments,('caller.mlirbc',))
    stage('futil',(tools['translate'],root/'binding/caller.mlirbc','--export-calyx','-o','kernel.futil'),('kernel.futil',))
    dependencies.update({str(p):digest(p) for p in library.rglob('*') if p.is_file()})
    stage('sv',(tools['calyx'],root/'futil/kernel.futil','-b','verilog',*CALYX_RESOURCE_PASSES,
        '-p','external','--synthesis','--nested','-l',library,'-o','kernel.sv'),('kernel.sv',))
    # The testbench and transport are simulator inputs only, after native RTL.
    simulator_inputs=[root/'sv/kernel.sv',tb]
    frames=None
    if transport != 'peer':
        primitives=Path(__file__).resolve().parents[2]/'rtl/primitives'
        simulator_inputs.extend(primitives/name for name in
            ('closure_ddr_read_port.sv','closure_ddr_read_bank.sv','closure_ddr_read_cdc.sv'))
    if transport=='bank-loaded-cdc':
        simulator_inputs.extend(primitives/name for name in
                                ('closure_ddr_image_loader.sv','closure_ddr_loaded_port.sv'))
        layout=materialize_ddr_images(manifest,root/'ddr-layout',data_width=576,address_width=25)
        dependencies.update(layout['dependency_hashes'])
        for path in (root/'ddr-layout/layout.json',root/'ddr-layout/images.bin'):
            dependencies[str(path)]=digest(path)
        frames=root/'frames.hex'
        frames.write_text(f"{int.from_bytes((root/'ddr-layout/images.bin').read_bytes(),'little'):0144x}\n")
        dependencies[str(frames)]=digest(frames)
    dependencies.update({str(path):digest(path) for path in simulator_inputs})
    stage('simulator',(tools['iverilog'],'-g2012','-s','tb','-o','simulator',*simulator_inputs),('simulator',))
    cases=[]
    configurations=[(delay,None) for delay in (1,7,29)] if transport == 'peer' else [
        (delay,clocks) for delay in (1,7,29) for clocks in ((17,6,1),(17,6,5),(6,17,3),(7,11,9))]
    configurations=[(delay,clocks,order) for delay,clocks in configurations
                    for order in ((0,1) if transport=='bank-loaded-cdc' else (None,))]
    for delay,clocks,order in configurations:
        name='case-'+str(delay)
        plusargs=['+DELAY='+str(delay)]
        if invalid_write:plusargs.append('+INVALID_WRITE=1')
        if corrupt_load:plusargs.append('+CORRUPT_LOAD=1')
        if clocks is not None:
            core,ui,phase=clocks
            name+=f'-core{core}-ui{ui}-phase{phase}'
            plusargs.extend((f'+CORE={core}',f'+UI={ui}',f'+PHASE={phase}'))
        if order is not None:
            name+=f'-order{order}';plusargs.append(f'+ORDER={order}')
        # Verification peers/loader read declared immutable inputs, never answers.
        (root/name).mkdir(exist_ok=True);(root/name/image.name).write_bytes(image.read_bytes())
        dependencies[str(root/name/image.name)]=digest(root/name/image.name)
        if frames is not None:
            (root/name/frames.name).write_bytes(frames.read_bytes())
            dependencies[str(root/name/frames.name)]=digest(root/name/frames.name)
        stage(name,(tools['vvp'],root/'simulator/simulator',*plusargs),('result.txt',))
        matches=re.findall(r'^RESULT (\d+) ([0-9a-fA-F]+) (\d+)$',(root/name/'result.txt').read_text(),re.M)
        if len(matches)!=1:raise ValueError('missing off-chip runtime observation')
        requests,value,cycles=matches[0]
        expected=int(image.read_text().splitlines()[2],16)
        if int(requests)!=2 or int(value,16)!=expected:raise ValueError('off-chip input image comparison failed')
        cases.append({'delay':delay,'requests':int(requests),'result':int(value,16),'cycles':int(cycles),
                      **({'clocks':clocks} if clocks is not None else {})})
        if order is not None:cases[-1]['write_handshake_order']=order
    for name in stages:verify_stage_receipt(root/name)
    result={'scope':scope,'cases':cases,'dependency_hashes':dependencies}
    _write_json(root/'qualification.json',result)
    return result
