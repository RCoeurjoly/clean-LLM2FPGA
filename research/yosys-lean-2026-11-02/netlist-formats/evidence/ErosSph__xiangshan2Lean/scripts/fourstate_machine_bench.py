"""Streaming original-RTL observer for large admitted v9 hierarchies.

The stimulus, stable force/release seed and pre/post observation contract follow
the F55/F56 harness. Grouping by owner avoids repeated whole-state scans. No
source module is changed, and every RAM word remains an explicit observation.
"""
from pathlib import Path
import json


def short_trace(mapping):
    inputs=[p for p in mapping['ports'] if p['direction']=='input']
    now={p['name']:'0'*p['width'] for p in inputs}
    for root in mapping['events']['roots']:now[root]='1'
    rows=[dict(inputs=dict(now))]
    def change(name,bits):
        if name not in now:raise ValueError('Required XSTop input missing: '+name)
        if len(bits)!=len(now[name]):raise ValueError('Stimulus width mismatch')
        if now[name]!=bits:now[name]=bits;rows.append(dict(inputs=dict(now)))
    def cycle(root):change(root,'0');change(root,'1')
    change('io_reset','1');cycle('io_clock');change('io_reset','0')
    for _ in range(4):cycle('io_clock')
    for symbol in '01xz':
        change('io_systemjtag_jtag_TMS',symbol)
        cycle('io_systemjtag_jtag_TCK')
    for symbol in 'xz':
        change('dma_0_w_bits_data',symbol*len(now['dma_0_w_bits_data']))
        change('dma_0_w_valid',symbol)
        cycle('io_clock')
    change('io_systemjtag_reset','1');cycle('io_systemjtag_jtag_TCK');change('io_systemjtag_reset','0')
    previous=None
    for row in rows:
        if previous is not None and sum(row['inputs'][n]!=previous[n] for n in now)>1:
            raise ValueError('Simultaneous changes in frozen trace')
        previous=row['inputs']
    return rows


def original_bench(mapping,trace,path):
    states=mapping['states'];processes=mapping['events']['processes']
    edges=[p for p in processes if p['kind']=='edge']
    groups={p['id']:[] for p in edges}
    for s in states:
        if s['process'] in groups:groups[s['process']].append(s)
    path=Path(path)
    with path.open('x',encoding='ascii',newline='\n') as f:
        def emit(text):f.write(text+'\n')
        emit('module tb;integer sample,idx,seed_arg,observations;reg seed_symbol,observe_events=0;')
        connections=[];names=[];width=0
        for i,p in enumerate(mapping['ports']):
            name=f'p{i}_name'
            emit(f'{"reg" if p["direction"]=="input" else "wire"} [{p["width"]-1}:0] {name};')
            connections.append(f'.{p["name"]}({name})')
            if p['direction']=='input':names.append(name);width+=p['width']
        emit(f'reg [{width-1}:0] vectors[0:{len(trace)-1}];')
        emit(mapping['top']+' dut('+','.join(connections)+');')
        def state_record(s,kind,owner=None):
            m=s.get('memory');obj=s['object_id'];value='dut.'+s['rtl_path']+('[idx]' if m else '')
            if m:emit(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)begin')
            index='idx' if m else '0'
            if owner is None:
                emit(f'$fdisplay(observations,"C2L %0d %0d S {obj} %0d %b",sample,phase,{index},{value});')
            else:
                emit(f'$fdisplay(observations,"A2L %0d {owner} %0d S {obj} %0d %b",sample,count{owner},{index},{value});')
            if m:emit('end')
        for p in edges:
            pid=p['id'];emit(f'integer count{pid}=0;task activation{pid};begin')
            for s in groups[pid]:state_record(s,'A2L',pid)
            emit('end endtask')
            terms=[('posedge' if t['edge']=='PosEdge' else 'negedge')+' dut.'+t.get('original_signal',t['signal']).split('.',1)[1] for t in p['triggers']]
            emit('always @('+' or '.join(terms)+f')begin count{pid}=count{pid}+1;if(observe_events)activation{pid};end')
        emit('task observe;input integer phase;begin')
        for i,p in enumerate(mapping['ports']):
            if p['direction']=='output':emit(f'$fdisplay(observations,"C2L %0d %0d O {i} 0 %b",sample,phase,p{i}_name);')
        for s in states:state_record(s,'C2L')
        for i,n in enumerate(mapping['events']['monitors']):
            emit(f'$fdisplay(observations,"C2L %0d %0d M {i} 0 %b",sample,phase,dut.{n.split(".",1)[1]});')
        for p in edges:emit(f'$fdisplay(observations,"C2L %0d %0d E {p["id"]} 0 %0d",sample,phase,count{p["id"]});')
        emit('end endtask initial begin if(!$value$plusargs("FILL=%d",seed_arg))$fatal(1,"explicit fill required");case(seed_arg)0:seed_symbol=0;1:seed_symbol=1;2:seed_symbol=1\'bx;3:seed_symbol=1\'bz;default:$fatal(1,"invalid fill");endcase')
        emit('observations=$fopen("observations.log","w");if(observations==0)$fatal(1,"observation file open failed");$readmemb("original-inputs.mem",vectors);')
        emit('{'+','.join(names)+'}=vectors[0];#2;')
        for s in states:
            if 'memory' not in s:emit(f'force dut.{s["rtl_path"]}={{{s["width"]}{{seed_symbol}}}};')
        emit('#1;')
        for s in states:
            m=s.get('memory')
            if m:emit(f'for(idx={m["offset"]};idx<{m["offset"]+m["depth"]};idx=idx+1)dut.{s["rtl_path"]}[idx]={{{s["width"]}{{seed_symbol}}}};')
        for s in states:
            if 'memory' not in s:emit('release dut.'+s['rtl_path']+';')
        emit('#0;')
        for p in edges:emit(f'count{p["id"]}=0;')
        emit(f'observe_events=1;for(sample=0;sample<{len(trace)};sample=sample+1)begin')
        emit('$display("F47_STEP %0d",sample);$fwrite(32\'h80000002,"F56_STDERR_STEP %0d\\n",sample);$fflush(32\'h80000002);$fflush();')
        emit('{'+','.join(names)+'}=vectors[sample];#0;#0;#0;#0;observe(0);#1;observe(1);$fflush(observations);end $fclose(observations);$finish;end endmodule')
    payload='\n'.join(''.join(row['inputs'][p['name']] for p in mapping['ports'] if p['direction']=='input') for row in trace)+'\n'
    return payload


def prepare(mapping,folder):
    folder=Path(folder);folder.mkdir()
    trace=short_trace(mapping)
    (folder/'trace.json').write_text(json.dumps(trace,separators=(',',':')))
    data=original_bench(mapping,trace,folder/'original-tb.sv')
    (folder/'original-inputs.mem').write_text(data)
    schema=dict(format='full-original-state-fill-v1',fills=['0','1','x','z'],states=len(mapping['states']),logical_ram_words=sum(s['memory']['depth'] for s in mapping['states'] if 'memory' in s),scalar_bits=sum(s['width'] for s in mapping['states'] if 'memory' not in s),memory_bits=sum(s['width']*s['memory']['depth'] for s in mapping['states'] if 'memory' in s),physical_padding='tag=0,value=0',helper_state='as generated, deterministic zero controls/history; first step seeds event history',time_zero='excluded; stable all-scalar force/drain, RAM restoration, release before sample 0',samples_per_fill=len(trace),planned_samples=len(trace)*4)
    (folder/'initial-schema.json').write_text(json.dumps(schema,indent=2))
    return schema
