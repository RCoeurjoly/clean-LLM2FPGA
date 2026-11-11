"""Private v7 native dual-rail arrays and a synchronous ordered-step controller.

The scalar operator encoder is reused unchanged. No FunctionalIR or Lean types
are added. The explicit exec clock is a protocol clock, not an original clock.
"""
from pathlib import Path
import copy
from fourstate_stateflow import signature
from fourstate_scalable_events import plan_events


def parse_scalable(source, top, clock, *, event_clocks=(), immediate_assertions=False, _eventful=False, _extensions=False):
    from fourstate_elaborated import parse
    ir=parse(Path(source),top,'sequential',clock,bounded_memories=True,bounded_events=True,
             event_clocks=event_clocks,immediate_assertions=immediate_assertions,_compact=True,_scalable=True,_eventful=_eventful,_extensions=_extensions)
    memories={m['name']:m for m in ir.get('memories',[])}
    ir['ownership']=[]
    for i,s in enumerate(ir['states']):
        p=ir['events']['processes'][s['process']];s['object_id']=i
        ir['ownership'].append(dict(name=s['name'],process=p['id'],kind='memory' if s['name'] in memories else 'scalar',
            source=s['source'],triggers=copy.deepcopy(p['triggers']),latch_root=p.get('root')))
        if s['name'] in memories:memories[s['name']]['writer_process']=p['id']
    ir['ownership_sha256']=signature(ir['ownership'])
    ir['storage']=dict(scalar_bits=sum(s['width'] for s in ir['states'] if s['name'] not in memories),
        memory_bits=sum(m['depth']*m['width'] for m in memories.values()),memory_objects=len(memories))
    ir['event_plan']=plan_events(ir)
    validate_scalable(ir,_eventful=_eventful,_extensions=_extensions)
    return ir


def validate_scalable(ir,*,_eventful=False,_extensions=False):
    if ir.get('format')!=('chisel2lean-fourstate-ir-v9' if _extensions else 'chisel2lean-fourstate-ir-v8' if _eventful else 'chisel2lean-fourstate-ir-v7'):raise ValueError('Expected explicit private scalable profile')
    states={s['name']:s for s in ir['states']};mem={m['name']:m for m in ir.get('memories',[])};processes=ir['events']['processes']
    if len(states)!=len(ir['states']) or set(ir['next_state'])!=set(states):raise ValueError('Incomplete/duplicate scalable state')
    if signature(ir['ownership'])!=ir['ownership_sha256']:raise ValueError('Ownership checksum mismatch')
    if {o['name'] for o in ir['ownership']}!=set(states) or len(ir['ownership'])!=len(states):raise ValueError('Incomplete scalable ownership')
    owned=[set(p['states']) for p in processes]
    for o in ir['ownership']:
        s=states[o['name']];pid=s['process']
        if not 0<=pid<len(processes):raise ValueError('Invalid owner process')
        p=processes[pid]
        if o['process']!=pid or o['triggers']!=p['triggers'] or o['latch_root']!=p.get('root') or s['name'] not in owned[pid]:raise ValueError('State clock/process ownership mismatch')
    for name,m in mem.items():
        if 'words' in m or 'indices' in m or m['depth']!=abs(m['left']-m['right'])+1 or m['writer_process']!=states[name]['process']:raise ValueError('Invalid compact memory object')
    versions={}
    for i,n in enumerate(ir['nodes']):
        if n['id']!=i or any(type(a) is not int or not 0<=a<i for a in n['args']):raise ValueError('Invalid compact DAG')
        if n['op']=='memory_ref':versions[i]=n['name']
        elif n['op']=='memory_write':
            if n['name'] not in mem or n['process']!=mem[n['name']]['writer_process'] or versions.get(n['args'][0])!=n['name']:raise ValueError('Memory writer/version ownership mismatch')
            versions[i]=n['name']
        elif n['op']=='memory_merge':
            a,b=n['args'][1:]
            if a not in versions or versions.get(a)!=versions.get(b):raise ValueError('Mixed memory version merge')
            versions[i]=versions[a]
    for name,i in ir['next_state'].items():
        if not 0<=i<len(ir['nodes']) or (name in mem and versions.get(i)!=name):raise ValueError('Invalid next-state version')
    if ir['event_plan']!=plan_events(ir):raise ValueError('Event plan mismatch')
    if [s['object_id'] for s in ir['states']]!=list(range(len(ir['states']))):raise ValueError('Object identity mismatch')


def scalar_kernel(ir):
    """Replace only memory reads by ports and export guarded write commands.

Reverse DAG reachability distributes branch selection without enumerating
paths. A pending write is enabled iff it belongs to the selected final version.
"""
    from fourstate_encode import encode
    mem={m['name']:m for m in ir.get('memories',[])};nodes=copy.deepcopy(ir['nodes']);writes=[]
    def add(op,args=(),**extra):
        n=dict(id=len(nodes),op=op,width=1,signed=False,args=list(args),source=nodes[0]['source']);n.update(extra);nodes.append(n);return n['id']
    one=add('const',bits='1');taken={}
    def truth(i):
        if i not in taken:taken[i]=add('case_eq',[i,one],operand_width=1,compare_signed=False)
        return taken[i]
    reach={ir['next_state'][m]:one for m in mem}
    def push(i,g):reach[i]=add('or',[reach[i],g]) if i in reach else g
    for n in reversed(ir['nodes']):
        if n['id'] not in reach:continue
        g=reach[n['id']]
        if n['op']=='memory_write':
            before,address,data=n['args'];writes.append(dict(node=n['id'],memory=n['name'],process=n['process'],
                order=n['id'],guard=g,address=address,data=data,positions=n['positions'],source=n['source']))
            push(before,g)
        elif n['op']=='memory_merge':
            c,a,b=n['args'];t=truth(c);push(a,add('and',[g,t]));push(b,add('and',[g,add('not',[t])]))
        elif n['op']!='memory_ref':raise ValueError('Invalid memory version')
    writes.sort(key=lambda x:x['order'])
    signals=[copy.deepcopy(s) for s in ir['signals'] if s['name'] not in mem]
    translated=[];mapped={};reads=[]
    for n in nodes:
        if n['op'] in ('memory_ref','memory_write','memory_merge'):continue
        nn=copy.deepcopy(n);nn['id']=len(translated)
        if n['op']=='memory_read':
            name='__c2l_read_'+str(n['id']);reads.append(dict(node=n['id'],name=name,memory=n['name'],address=n['args'][0],width=n['width']))
            signals.append(dict(name=name,port_name=name,direction='input',width=n['width'],signed=False,left=n['width']-1,right=0,source=n['source']))
            nn.update(op='ref',args=[],name=name)
        else:nn['args']=[mapped[a] for a in n['args']]
        translated.append(nn);mapped[n['id']]=nn['id']
    assignments={name:mapped[i] for name,i in ir['assignments'].items()};order=list(ir['order'])
    for w in writes:
        for field in ('guard','address','data'):
            i=w[field];name=f'__c2l_write_{w["node"]}_{field}';ww=nodes[i]['width']
            signals.append(dict(name=name,port_name=name,direction='output',width=ww,signed=False,left=ww-1,right=0,source=w['source']))
            assignments[name]=mapped[i];order.append(name);w[field+'_port']=name;w[field+'_width']=ww
        w['address_signed']=nodes[w['address']].get('signed',False)
    old=dict(ir,format='chisel2lean-fourstate-ir-v5',top=ir['top']+'__kernel',signals=signals,nodes=translated,
        states=[s for s in ir['states'] if s['name'] not in mem],next_state={s:mapped[i] for s,i in ir['next_state'].items() if s not in mem},
        assignments=assignments,order=order,capabilities=['bounded_events'],clock_signals=[])
    old['assertions']=[dict(a,enable_node=mapped[a['enable_node']],pass_node=mapped[a['pass_node']]) for a in ir.get('assertions',[])]
    for a in old['assertions']:
        if 'arguments' in a:a['arguments']=[dict(v,node=mapped[v['node']]) for v in a['arguments']]
    rtl,mapping=encode(old)
    return rtl,mapping,reads,writes


def encode_scalable(ir,*,_eventful=False,_extensions=False,_shallow_memory=False,_compact_forwarding=False):
    validate_scalable(ir,_eventful=_eventful,_extensions=_extensions)
    kernel,km,reads,writes=scalar_kernel(ir);mem={m['name']:m for m in ir.get('memories',[])}
    states=copy.deepcopy(ir['states']);ss={s['name']:s for s in states};ports=[];connections=[];decl=[]
    from collections import defaultdict
    writes_by_memory=defaultdict(list)
    for w in writes:writes_by_memory[w['memory']].append(w)
    original_inputs={s['port_name'] for s in ir['signals'] if s['direction']=='input'}
    original_outputs={s['port_name'] for s in ir['signals'] if s['direction']=='output'}
    command_names={w[k+'_port'] for w in writes for k in ('guard','address','data')}
    read_names={r['name'] for r in reads}
    inputs=[p for p in km['ports'] if p['direction']=='input' and p['name'] in original_inputs]
    outputs=[p for p in km['ports'] if p['direction']=='output' and p['name'] in original_outputs]
    for j,p in enumerate(inputs):
        for key in ('tag','value'):
            ports.append(f'input wire [{p["width"]-1}:0] {p[key]}')
            name=f'in{j}_{key}';decl.append(f'reg [{p["width"]-1}:0] {name} = 0;');connections.append(f'.{p[key]}({name})')
    for p in outputs:
        for key in ('tag','value'):
            ports.append(f'output wire [{p["width"]-1}:0] {p[key]}');connections.append(f'.{p[key]}({p[key]})')
    max_width=max([1]+[s['width'] for s in states]);max_addr=max([1]+[(max(2,m['depth'])-1).bit_length() for m in mem.values()]);ow=max(1,(len(states)-1).bit_length())
    proc=ir['events']['processes'];mon=ir['events']['monitors'];P=len(proc);M=len(mon)
    ports+=['input wire exec_clk','input wire step_valid','output wire step_ready','output wire pre_valid','output wire post_valid',
        'output reg [3:0] fault = 0',f'output reg [{P-1}:0] fired = 0',f'input wire [{ow-1}:0] obs_object',
        f'input wire [{max_addr-1}:0] obs_index',f'output wire [{max_width-1}:0] obs_tag, obs_value',f'output wire [{2*M-1}:0] observed_monitors']
    scalar_map={s['name']:s for s in km['states']}
    for s in states:
        j=s['object_id'];w=s['width'];s.update(bank_tag=f'bank{j}_t',bank_value=f'bank{j}_v')
        if s['name'] in mem:
            m=mem[s['name']];aw=max(1,(max(2,m['depth'])-1).bit_length());mcopy=dict(m,physical_depth=1<<aw,address_width=aw,offset=min(m['left'],m['right']))
            s['memory']=mcopy
            for key in ('bank_tag','bank_value'):decl.append(f'(* keep *) reg [{w-1}:0] {s[key]} [0:{(1<<aw)-1}];')
        else:
            for key in ('bank_tag','bank_value'):decl.append(f'(* keep *) reg [{w-1}:0] {s[key]};')
            for key,bank in (('current_tag','bank_tag'),('current_value','bank_value')):connections.append(f'.{scalar_map[s["name"]][key]}({s[bank]})')
            for key in ('next_tag','next_value'):
                n=f'next{j}_{key}';decl.append(f'wire [{w-1}:0] {n};');connections.append(f'.{scalar_map[s["name"]][key]}({n})')
    commands={}
    for p in km['ports']:
        if p['name'] not in command_names:continue
        for key in ('tag','value'):
            name=p[key];decl.append(f'wire [{p["width"]-1}:0] {name};');connections.append(f'.{name}({name})')
        commands[p['name']]=p
    read_ports={p['name']:p for p in km['ports'] if p['name'] in read_names}
    # Address values for memory reads must be exported from the scalar network.
    # Reuse the command export mechanism through the private adapter helper.
    if reads:
        kernel,km,read_addresses=_export_read_addresses(ir,kernel,km,reads)
        for p in read_addresses.values():
            for key in ('tag','value'):
                decl.append(f'wire [{p["width"]-1}:0] {p[key]};');connections.append(f'.{p[key]}({p[key]})')
        for r in reads:
            s=ss[r['memory']];j=s['object_id'];m=s['memory'];p=read_addresses[r['node']];rp=read_ports[r['name']]
            valid,idx=_address_expr(p,ir['nodes'][r['address']].get('signed',False),m)
            safe=f'read{r["node"]}_addr';decl.append(f'wire [{m["address_width"]-1}:0] {safe} = {valid} ? {idx} : 0;')
            for key,bank,bad in (('tag','bank_tag',f"{s['width']}'b"+'1'*s['width']),('value','bank_value',"0")):
                n=rp[key];decl.append(f'wire [{s["width"]-1}:0] {n} = {valid} ? {s[bank]}[{safe}] : {bad};');connections.append(f'.{n}({n})')
    for j,m in enumerate(km['monitors']):
        for key in ('tag','value'):
            decl.append(f'wire {m[key]};');connections.append(f'.{m[key]}({m[key]})')
        decl.append(f'assign observed_monitors[{2*j} +: 2] = {{{m["tag"]},{m["value"]}}};')
    assertions=[]
    if km.get('assertions'):
        from fourstate_assertions import FIELDS
        for a in km['assertions']:
            for key in FIELDS:ports.append(f'output wire {a[key]}');connections.append(f'.{a[key]}({a[key]})')
            for arg in a.get('arguments',[]):
                for key in ('tag','value'):ports.append(f'output wire [{arg["width"]-1}:0] {arg[key]}');connections.append(f'.{arg[key]}({arg[key]})')
        assertions=km['assertions']
    if _eventful:
        rw=max(1,(ir['events']['max_waves']-1).bit_length())
        ports+=['output wire wave_valid',f'output reg [{rw-1}:0] wave_id = 0']
    lines=[f'module {ir["top"]}__scalable('+','.join(ports)+');',*decl,
        f'{km["encoded_top"]} kernel('+','.join(connections)+');',
        'reg [2:0] phase = 0; reg seeded = 0;',f'reg [{2*M-1}:0] history = 0, early = 0, before_nba = 0;',
        'assign step_ready=(phase==0 && fault==0); assign pre_valid=(phase==4 && fault==0); assign post_valid=(phase==6 && fault==0);']
    mid={n:i for i,n in enumerate(mon)}
    def fire(p,old):
        terms=[]
        for t in p['triggers']:
            a=f'{old}[{2*mid[t["signal"]]} +: 2]';b=f'observed_monitors[{2*mid[t["signal"]]} +: 2]'
            if t['edge']=='NegEdge':a,b=b,a
            terms.append(f'(({a}==0 && {b}!=0) || (({a}==2 || {a}==3) && {b}==1))')
        return '('+' || '.join(terms)+')'
    latch_states=[s for s in states if proc[s['process']]['kind']=='latch']
    changed=' || '.join(f'({s[bank]} != next{s["object_id"]}_{nxt})' for s in latch_states for bank,nxt in (('bank_tag','next_tag'),('bank_value','next_value'))) or "1'b0"
    # Balanced total muxes avoid proc-generated full_case don't-care defaults
    # and keep observation depth logarithmic in the number of objects.
    for key,bank in (('tag','bank_tag'),('value','bank_value')):
        level=[f'{s[bank]}[obs_index[{s["memory"]["address_width"]-1}:0]]' if 'memory' in s else s[bank] for s in states]
        level+=['0']*((1<<ow)-len(level));bit=0
        while len(level)>1:
            next_level=[]
            for k in range(0,len(level),2):
                name=f'observe_{key}_{bit}_{k//2}'
                lines.append(f'wire [{max_width-1}:0] {name}=obs_object[{bit}]?{level[k+1]}:{level[k]};');next_level.append(name)
            level=next_level;bit+=1
        lines.append(f'assign obs_{key}={level[0]};')
    changes=' + '.join(f'{{31\'b0,((in{j}_tag!={p["tag"]})||(in{j}_value!={p["value"]}))}}' for j,p in enumerate(inputs)) or '0'
    repeated=' || '.join(f'(fired[{p["id"]}] && {fire(p,"early")})' for p in proc if p['kind']=='edge') or "1'b0"
    lines += [f'wire bad_inputs=seeded && (({changes})>1);',f'wire bad_initial=!seeded && ({changed});',
        f'wire bad_latch=({changed});',f'wire bad_repeat=seeded && ({repeated});',
        'wire bad_nba=(observed_monitors!=before_nba);','wire accept=step_ready && step_valid && !bad_inputs;',
        'wire [3:0] fault_next=(phase==0 && step_valid && bad_inputs)?1:(phase==2 && bad_initial)?2:(phase==3 && bad_latch)?3:(phase==3 && bad_repeat)?4:(phase==5 && bad_nba)?5:(phase==7)?6:0;',
        'wire [2:0] phase_next=(phase==0)?(step_valid?(bad_inputs?7:1):0):(phase==1)?2:(phase==2)?(bad_initial?7:3):(phase==3)?((bad_latch||bad_repeat)?7:4):(phase==4)?5:(phase==5)?(bad_nba?7:6):(phase==6)?0:7;',
        'always @(posedge exec_clk) begin',
        'fault<=fault!=0?fault:fault_next;phase<=fault!=0?phase:phase_next;',
        'seeded<=((fault==0)&&(phase==6))?1:seeded;',
        'early<=((fault==0)&&(phase==1))?observed_monitors:early;',
        'before_nba<=((fault==0)&&(phase==3))?observed_monitors:before_nba;',
        'history<=((fault==0)&&(phase==6))?observed_monitors:history;']
    for j,p in enumerate(inputs):
        for key in ('tag','value'):lines.append(f'in{j}_{key}<=accept?{p[key]}:in{j}_{key};')
    for p in proc:
        pid=p['id']
        if p['kind']=='latch':lines.append(f'fired[{pid}]<=0;')
        else:lines.append(f'fired[{pid}]<=(fault!=0)?fired[{pid}]:(phase==0 && accept)?0:(phase==1)?(seeded && {fire(p,"history")}):(phase==3)?(fired[{pid}] || (seeded && {fire(p,"early")})):fired[{pid}];')
    lines.append('end')
    if _eventful:
        from fourstate_eventful import controller
        # Storage, operators and observation muxes are shared. Replace only the
        # explicit protocol controller; the v7 text stays byte-for-byte stable.
        start=next(i for i,l in enumerate(lines) if l.startswith('wire bad_inputs='))
        common=[l for l in lines[:start] if not l.startswith('assign step_ready=')]
        lines=common+controller(ir,inputs,changed,fire)
    for s in states:
        j=s['object_id'];p=proc[s['process']]
        if 'memory' not in s:
            enable='phase==2 && (seeded || !('+changed+'))' if p['kind']=='latch' else f'phase==4 && fired[{p["id"]}]'
            lines.append(f'always @(posedge exec_clk) begin {s["bank_tag"]}<=(fault==0 && ({enable}))?next{j}_next_tag:{s["bank_tag"]};{s["bank_value"]}<=(fault==0 && ({enable}))?next{j}_next_value:{s["bank_value"]};end')
        else:
            pending=[];updates=[]
            group=writes_by_memory[s['name']]
            addresses=[ir['nodes'][w['address']] for w in group]
            # Only identical bound references (including type) qualify. Under
            # the explicit binary physical-rail contract their safe indices
            # agree even for logical X/Z (invalid -> zero, disabled write).
            same_ref=bool(_compact_forwarding and addresses and all(n['op']=='ref' and
                (n['name'],n['width'],n.get('signed',False))==
                (addresses[0]['name'],addresses[0]['width'],addresses[0].get('signed',False)) for n in addresses))
            for w in writes_by_memory[s['name']]:
                g,a,d=(commands[w[k+'_port']] for k in ('guard','address','data'))
                valid,idx=_address_expr(a,w['address_signed'],s['memory'])
                stem='write'+str(w['node']);aw=s['memory']['address_width'];width=s['width']
                lines.append(f'wire [{aw-1}:0] {stem}_addr={valid}?{idx}:0;')
                enable=f'(fault==0 && phase==4 && fired[{p["id"]}] && !{g["tag"]} && {g["value"]} && {valid})'
                for bank,key in (('bank_tag','tag'),('bank_value','value')):
                    before=f'{s[bank]}[{stem}_addr]'
                    if same_ref and pending:before=f'{pending[-1]}_{key}'
                    for k,prev in enumerate([] if same_ref else pending):
                        expression=f'(({prev}_addr=={stem}_addr)?{prev}_{key}:{before})'
                        if _shallow_memory:
                            # Preserve the exact four-state mux and NBA priority,
                            # but give each forwarding stage an ordinary wire.
                            # Deep per-word bit-write lists must not become one
                            # recursively nested source expression.
                            name=f'{stem}_forward{k}_{key}'
                            lines.append(f'wire [{width-1}:0] {name}={expression};')
                            before=name
                        else:before=expression
                    lines.append(f'wire [{width-1}:0] {stem}_old_{key}={before};')
                    if w['positions'] is None:new=d[key]
                    else:
                        positions=w['positions'];bits=[]
                        for bit in reversed(range(width)):
                            bits.append(f'{d[key]}[{len(positions)-1-positions.index(bit)}]' if bit in positions else f'{stem}_old_{key}[{bit}]')
                        new='{'+','.join(bits)+'}'
                    lines.append(f'wire [{width-1}:0] {stem}_{key}={enable}?{new}:{stem}_old_{key};')
                    updates.append(f'{s[bank]}[{stem}_addr]<={stem}_{key};')
                pending.append(stem)
            # Total write operands avoid native-X inactive memory-port values
            # in the unchanged public undef-exposure pipeline. Disabled writes
            # forward the pending old word, including prior same-address writes.
            lines.append('always @(posedge exec_clk) begin '+' '.join(updates)+' end')
    lines.append('endmodule')
    mapping=dict(format='chisel2lean-fourstate-scalable-ports-v1',top=ir['top'],encoded_top=ir['top']+'__scalable',
        ports=[*inputs,*outputs],states=states,events=ir['events'],event_plan=ir['event_plan'],assertions=assertions,
        observation=dict(width=max_width,index_width=max_addr,object_width=ow),writes=writes,
        protocol=dict(clock='exec_clk',ready='step_ready',pre='pre_valid',post='post_valid',fault='fault',initial_helper='phase/seeded/fired/history/early/before_nba=0; fault=0',padding='explicit tag=0,value=0; logically inaccessible'))
    if _eventful:
        mapping['format']='chisel2lean-fourstate-eventful-ports-v1'
        mapping['protocol'].update(wave='wave_valid',wave_id='wave_id',wave_id_width=rw,max_waves=ir['events']['max_waves'],initial_helper='phase/seeded/fired/history/early/before_nba/wave_id=0; fault=0')
    if _extensions:mapping['format']='chisel2lean-fourstate-extended-ports-v1'
    return kernel+'\n'+'\n'.join(lines)+'\n',mapping


def _address_expr(p,signed,m):
    t,v=p['tag'],p['value'];w=p['width'];lo=m['offset'];hi=lo+m['depth']-1
    # Compare before subtraction/truncation; SV's unsigned extension is safe
    # because negative signed addresses are separately excluded.
    valid=f'((~(|{t})) && '+(f'!{v}[{w-1}] && ' if signed else '')+f'({v}>={lo}) && ({v}<={hi}))'
    return valid,f'{m["address_width"]}\'({v}-{lo})'


def _export_read_addresses(ir,kernel,km,reads):
    """Export read address expressions through the same unchanged scalar encoder."""
    # Add temporary ordinary output assignments to a private IR copy and reuse
    # scalar_kernel; no public backend, parser or operator semantics change.
    work=copy.deepcopy(ir)
    for r in reads:
        n=ir['nodes'][r['address']];name='__c2l_address_'+str(r['node'])
        work['signals'].append(dict(name=name,port_name=name,direction='output',width=n['width'],signed=False,left=n['width']-1,right=0,source=n['source']))
        work['assignments'][name]=r['address'];work['order'].append(name)
    rtl,mapping,_,_=scalar_kernel(work)
    # Address ports are internal to the wrapper, not original physical ports.
    return rtl,mapping,{r['node']:next(p for p in mapping['ports'] if p['name']=='__c2l_address_'+str(r['node'])) for r in reads}
