"""Verification-only helpers; never imported by the compiler pipeline."""
import hashlib
from pathlib import Path
import re


def install_frozen_images(bindings, source_dir, runtime_dir):
    """Authenticate all declared frozen images before installing any of them."""
    source_dir, runtime_dir = Path(source_dir), Path(runtime_dir)
    images = {}
    for binding in bindings:
        filename = binding.get('init_file')
        if not filename:
            continue
        relative = Path(filename)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Frozen image path must stay within its image directory')
        expected = binding['init_sha256']
        if filename in images and images[filename][0] != expected:
            raise ValueError('Conflicting frozen image identities')
        content = (source_dir / relative).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Frozen simulation image differs from compiler input: ' + filename)
        images[filename] = (expected, content)
    for filename, (_, content) in images.items():
        target = runtime_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {name: expected for name, (expected, _) in images.items()}


def decode_output(stdout, words, width, *, prefix="RESULT"):
    """Require every output word exactly once, in interface order."""
    if '$readmem file not found' in stdout:
        raise ValueError('Simulation lacks a frozen memory image')
    if prefix not in ('RESULT', 'TOKEN', 'LOGIT'):
        raise ValueError('Unknown output role')
    lines = [line for line in stdout.splitlines() if line.startswith(prefix + ' ')]
    matches = [re.fullmatch(prefix + r' (\d+) ([0-9a-fA-F]+)', line) for line in lines]
    if any(match is None for match in matches):
        raise ValueError('Malformed simulation output')
    matches = [match.groups() for match in matches]
    if len(matches) != words or [int(index) for index, _ in matches] != list(range(words)):
        raise ValueError('Incomplete, duplicated or reordered simulation output')
    result = [int(value, 16) for _, value in matches]
    if any(value >= 1 << width for value in result):
        raise ValueError('Simulation output exceeds interface word width')
    return result



_PUBLIC_MEMORY_MODULE = '''module verification_memory #(
 parameter WORDS=4, ADDR=2, PRELOADED=0
)(input clk, reset, content_en, write_en,
 input [ADDR-1:0] addr0, input [63:0] write_data,
 output reg [63:0] read_data=0, output reg done=0);
 reg [63:0] memory[0:WORDS-1]; reg [WORDS-1:0] written='0;
 reg pending=0, pending_read=0; reg [ADDR-1:0] pending_addr=0;
 initial if (PRELOADED) begin $readmemh("prompt.hex",memory); written='1; end
 always @(posedge clk) begin
  if(reset) begin done<=0;pending<=0;pending_read<=0;read_data<=0; end
  else begin
   done<=pending;
   if(pending_read) read_data<=memory[pending_addr];
   pending<=content_en; pending_read<=content_en && !write_en;
   if(content_en) begin
    if($isunknown(addr0) || $isunknown(write_en) || addr0>=WORDS) $fatal(1,"invalid public memory request");
    pending_addr<=addr0;
    if(write_en) begin
     if(PRELOADED) $fatal(1,"prompt memory is read-only");
     memory[addr0]<=write_data;written[addr0]<=1;
    end else if(!written[addr0]) $fatal(1,"public memory read before write");
   end
  end
 end
endmodule
'''

def emit_generation_testbench(logit_words, *, max_cycles=2_000_000_000):
    """Verification-only memory models for the three declared public roles.

    Expected answers never enter this bench. Prompt data is loaded at runtime;
    tokens/logits are observed only after the accelerator reports completion.
    """
    if type(logit_words) is not int or not 0 < logit_words <= 2**31:
        raise ValueError('invalid public logits geometry')
    if type(max_cycles) is not int or not 0 < max_cycles < 2**63:
        raise ValueError('invalid simulation cycle limit')
    memory = _PUBLIC_MEMORY_MODULE
    body = ['module tb; reg clk=0; always #5 clk=~clk;',
            'reg reset=1,go=0; wire done; longint unsigned cycles=0; integer i;']
    connections = ['.clk(clk)', '.reset(reset)', '.go(go)', '.done(done)']
    for role, words in (('prompt', 4), ('generated_tokens', 16), ('last_logits', logit_words)):
        bits = max(1, (words-1).bit_length())
        body += [f'wire [{bits-1}:0] {role}_addr0;',
                 f'wire {role}_clk,{role}_reset,{role}_content_en,{role}_write_en,{role}_done;',
                 f'wire [63:0] {role}_write_data,{role}_read_data;']
        suffixes = ('clk','reset','addr0','content_en','write_en','write_data','read_data','done')
        ports = ','.join(f'.{suffix}({role}_{suffix})' for suffix in suffixes)
        body.append(f'verification_memory #(.WORDS({words}),.ADDR({bits}),.PRELOADED({int(role=="prompt")})) {role}_model({ports});')
        connections += [f'.{role}_{suffix}({role}_{suffix})' for suffix in suffixes]
    body += ['main dut('+','.join(connections)+');', 'initial begin',
             'repeat(4) @(negedge clk); reset=0;go=1;',
             f'while(!done && cycles<{max_cycles}) begin @(negedge clk);cycles=cycles+1;end',
             'if(!done) $fatal(1,"generation timeout");',
             'for(i=0;i<16;i=i+1) begin',
             'if(!generated_tokens_model.written[i]) $fatal(1,"unwritten generated token");',
             '$display("TOKEN %0d %016h",i,generated_tokens_model.memory[i]);end',
             f'for(i=0;i<{logit_words};i=i+1) begin',
             'if(!last_logits_model.written[i]) $fatal(1,"unwritten final logit");',
             '$display("LOGIT %0d %016h",i,last_logits_model.memory[i]);end',
             '$display("CYCLES %0d",cycles);$finish;end endmodule']
    return memory + '\n'.join(body) + '\n'


def emit_loaded_generation_testbench(logit_words, frame_count, *, max_cycles=2_000_000_000):
    """Observe full generation through the real loader/bank/CDC RTL.

    The native-memory peer models command/data ordering and delayed responses.
    It loads only input frames; model answers are supplied to a separate checker.
    Physical controller, PHY and board timing are outside this behavioral peer.
    """
    emit_generation_testbench(logit_words, max_cycles=max_cycles)
    if type(frame_count) is not int or not 0 < frame_count <= 2**25:
        raise ValueError('invalid native frame geometry')
    body = ['module tb;', f'localparam integer FRAMES={frame_count};',
        'reg clk=0,ui_clk=0;always #5 clk=~clk;always #3 ui_clk=~ui_clk;',
        'reg reset=1,go=0,controller_ready=0;wire done;',
        'longint unsigned cycles=0;integer i,host_index=0;',
        'wire fault,input_valid,input_ready,ui_loaded,ui_load_fault,core_fault,core_memory_ready;',
        'wire [24:0] frame_index,cmd_addr;wire cmd_valid,cmd_we,wdata_valid,rdata_ready;',
        'wire [575:0] input_data,wdata;wire [71:0] wdata_we;',
        'reg cmd_ready=0,wdata_ready=0,rdata_valid=0;reg [575:0] rdata=0;',
        'reg [575:0] input_frames[0:FRAMES-1],memory[0:FRAMES-1],held_data=0;',
        "reg [FRAMES-1:0] written='0;reg command_pending=0,data_pending=0,read_pending=0;",
        'reg [24:0] held_addr=0,read_addr=0;integer delay_cycles=2,count=0,order=0;',
        'integer writes=0,load_reads=0,inference_reads=0;',
        'assign input_valid=controller_ready&&host_index<FRAMES;',
        "assign input_data=host_index<FRAMES?input_frames[host_index]:576'd0;"]
    connections = ['.clk(clk)', '.reset(reset)', '.go(go)', '.done(done)']
    names = ('ui_clk','controller_ready','input_valid','input_ready','input_data',
             'frame_index','ui_loaded','ui_load_fault','core_fault','core_memory_ready',
             'cmd_valid','cmd_ready','cmd_we','cmd_addr','wdata_valid','wdata_ready',
             'wdata','wdata_we','rdata_valid','rdata_ready','rdata','fault')
    connections += [f'.{name}({name})' for name in names]
    for role, words in (('prompt',4),('generated_tokens',16),('last_logits',logit_words)):
        bits=max(1,(words-1).bit_length())
        body += [f'wire [{bits-1}:0] {role}_addr0;',
                 f'wire {role}_clk,{role}_reset,{role}_content_en,{role}_write_en,{role}_done;',
                 f'wire [63:0] {role}_write_data,{role}_read_data;']
        suffixes=('clk','reset','addr0','content_en','write_en','write_data','read_data','done')
        ports=','.join(f'.{suffix}({role}_{suffix})' for suffix in suffixes)
        body.append(f'verification_memory #(.WORDS({words}),.ADDR({bits}),.PRELOADED({int(role=="prompt")})) {role}_model({ports});')
        connections += [f'.{role}_{suffix}({role}_{suffix})' for suffix in suffixes]
    body += ['graph_ddr_bank dut('+','.join(connections)+');', '''
 initial begin
  $readmemh("frames.hex",input_frames);
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=2;
  if(!$value$plusargs("ORDER=%d",order))order=0;
  if(delay_cycles<0||order<0||order>1)$fatal(1,"invalid native peer configuration");
  repeat(7)@(negedge clk);reset=0;
  repeat(7)@(negedge ui_clk);controller_ready=1;
  wait(core_memory_ready);@(negedge clk);go=1;
  wait(done);@(negedge clk);
  if(writes!=FRAMES||load_reads!=FRAMES||inference_reads==0)
   $fatal(1,"incomplete native loading or inference transactions");
  for(i=0;i<16;i=i+1)begin
   if(!generated_tokens_model.written[i])$fatal(1,"unwritten generated token");
   $display("TOKEN %0d %016h",i,generated_tokens_model.memory[i]);end
''', f'for(i=0;i<{logit_words};i=i+1)begin', '''
   if(!last_logits_model.written[i])$fatal(1,"unwritten final logit");
   $display("LOGIT %0d %016h",i,last_logits_model.memory[i]);end
  $display("NATIVE %0d %0d %0d",writes,load_reads,inference_reads);
  $display("CYCLES %0d",cycles);$finish;
 end
 always @(negedge clk)if(!reset)begin
  cycles<=cycles+1;
''', f'if(cycles>={max_cycles})$fatal(1,"generation timeout");', '''
  if(fault||core_fault||ui_load_fault)$fatal(1,"loaded transport fault");
  if(ui_loaded&&(writes!=FRAMES||load_reads!=FRAMES))
   $fatal(1,"unverified image loading");
  if(done&&!core_memory_ready)$fatal(1,"completion before verified loading");
 end
 always @(posedge ui_clk)begin
  if(reset)host_index<=0;
  else if(input_valid&&input_ready)begin
   if(frame_index!=host_index)$fatal(1,"host image frame order changed");
   host_index<=host_index+1;
  end
 end
 always @(negedge ui_clk)begin
  cmd_ready=controller_ready&&!reset&&!command_pending&&!read_pending&&
            (!cmd_we||order==0||data_pending);
  wdata_ready=controller_ready&&!reset&&!data_pending&&(order==1||command_pending);
 end
 always @(posedge ui_clk)begin
  if(reset)begin
   command_pending<=0;data_pending<=0;read_pending<=0;rdata_valid<=0;
   writes<=0;load_reads<=0;inference_reads<=0;written<='0;
  end else begin
   if(cmd_valid&&cmd_ready)begin
    if(cmd_addr>=FRAMES)$fatal(1,"native address exceeds image");
    if(cmd_we)begin
     if(ui_loaded)$fatal(1,"immutable inference write");
     held_addr<=cmd_addr;command_pending<=1;
    end else begin
     if(!written[cmd_addr])$fatal(1,"native read before completed image write");
     read_addr<=cmd_addr;read_pending<=1;count<=delay_cycles+cmd_addr%3;
     if(ui_loaded)inference_reads<=inference_reads+1;else load_reads<=load_reads+1;
    end
   end
   if(wdata_valid&&wdata_ready)begin
    if(wdata_we!=={72{1'b1}})$fatal(1,"partial native image write");
    held_data<=wdata;data_pending<=1;
   end
   if(command_pending&&data_pending)begin
    memory[held_addr]<=held_data;written[held_addr]<=1;writes<=writes+1;
    command_pending<=0;data_pending<=0;
   end
   if(read_pending&&!command_pending&&!data_pending&&!rdata_valid)begin
    if(count==0)begin rdata<=memory[read_addr];rdata_valid<=1;end
    else count<=count-1;
   end
   if(rdata_valid&&rdata_ready)begin rdata_valid<=0;read_pending<=0;end
  end
 end
endmodule
''']
    return _PUBLIC_MEMORY_MODULE+'\n'.join(body)+'\n'
