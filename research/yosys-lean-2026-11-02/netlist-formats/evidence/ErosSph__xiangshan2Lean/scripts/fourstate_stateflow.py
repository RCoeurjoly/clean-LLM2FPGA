"""F5.3 private compact state IR. Never invokes FunctionalIR or Lean.

Memory versions are pending NBA updates, not readable storage. All memory_read
nodes read the activation's old snapshot, including after an earlier NBA.
Only explicit small-model lowering enumerates words; import never does.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path


def signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def parse_compact(source,top,clock,*,event_clocks=(),immediate_assertions=False):
    from fourstate_elaborated import parse
    ir=parse(Path(source),top,'sequential',clock,bounded_memories=True,bounded_events=True,
        event_clocks=event_clocks,immediate_assertions=immediate_assertions,_compact=True)
    processes=ir['events']['processes']
    owners=[]
    memories={m['name']:m for m in ir.get('memories',[])}
    for state in ir['states']:
        p=processes[state['process']]
        owner=dict(name=state['name'],process=p['id'],kind='memory' if state['name'] in memories else 'scalar',
            source=state['source'],triggers=copy.deepcopy(p['triggers']),latch_root=p.get('root'))
        owners.append(owner)
        if state['name'] in memories:memories[state['name']]['writer_process']=p['id']
    ir['ownership']=owners
    ir['ownership_sha256']=signature(owners)
    ir['state_groups']=[dict(process=p['id'],objects=list(p['states'])) for p in processes]
    ir['storage']=dict(scalar_bits=sum(s['width'] for s in ir['states'] if s['name'] not in memories),
        memory_bits=sum(m['depth']*m['width'] for m in memories.values()),
        memory_objects=len(memories),memory_read_nodes=sum(n['op']=='memory_read' for n in ir['nodes']),
        memory_write_nodes=sum(n['op']=='memory_write' for n in ir['nodes']))
    validate_compact(ir)
    return ir


def validate_compact(ir):
    if ir.get('format')!='chisel2lean-fourstate-ir-v6':raise ValueError('Expected private compact v6')
    if signature(ir['ownership'])!=ir['ownership_sha256']:raise ValueError('Ownership checksum mismatch')
    states={s['name']:s for s in ir['states']};mem={m['name']:m for m in ir.get('memories',[])}
    processes=ir['events']['processes']
    if set(ir['next_state'])!=set(states):raise ValueError('Incomplete compact next state')
    if len(ir['ownership'])!=len(states):raise ValueError('Incomplete compact ownership')
    for owner in ir['ownership']:
        state=states[owner['name']];p=processes[state['process']]
        if (owner['process']!=state['process'] or owner['triggers']!=p['triggers'] or
            owner['latch_root']!=p.get('root') or owner['name'] not in p['states']):
            raise ValueError('State clock/process ownership mismatch')
    for name,m in mem.items():
        if ('words' in m or 'indices' in m or m['depth']!=abs(m['left']-m['right'])+1 or
            m['writer_process']!=states[name]['process']):raise ValueError('Invalid compact memory object')
    for i,n in enumerate(ir['nodes']):
        if n['id']!=i or any(type(a) is not int or not 0<=a<i for a in n['args']):raise ValueError('Invalid compact DAG')
        if n['op']=='memory_write' and (n['name'] not in mem or n['process']!=mem[n['name']]['writer_process']):
            raise ValueError('Memory writer ownership mismatch')


def lower_small(ir,*,allowed_tops):
    """Explicit test/deployment selection, not a new general memory backend."""
    validate_compact(ir)
    if ir['top'] not in set(allowed_tops):raise ValueError('Top not explicitly selected for small-model expansion')
    out=copy.deepcopy(ir);mem={m['name']:m for m in out.get('memories',[])}
    states={s['name']:s for s in out['states']};words={};signals={s['name']:s for s in out['signals']}
    for name,m in mem.items():
        m['indices']=list(range(min(m['left'],m['right']),max(m['left'],m['right'])+1));m['words']=[]
        words[name]={}
        for index in m['indices']:
            w=dict(states[name]);w.pop('memory_object',None)
            path=f'{name}[{index}]';w.update(name=path,logical_path=path,rtl_path=m['rtl_path']+f'[{index}]',memory=name,memory_index=index)
            w['bit_indices']=[w['right']+(i if w['left']>=w['right'] else -i) for i in range(w['width'])]
            words[name][index]=w;m['words'].append(path)
    out['states']=[w for s in out['states'] for w in (list(words[s['name']].values()) if s['name'] in mem else [s])]
    out['signals']=[w for s in out['signals'] for w in (list(words[s['name']].values()) if s['name'] in mem else [s])]
    nodes=[];mapped={}
    def add(origin,op,w,args=(),**extra):
        n=dict(origin);n.update(id=len(nodes),op=op,width=w,args=list(args));n.pop('positions',None);n.pop('process',None)
        if op not in ('ref','select'):n.pop('name',None)
        n.update(extra);nodes.append(n);return n['id']
    def match(origin,address,index):
        a=nodes[address];w=a['width']
        if index >= 1<<(w-1 if a['signed'] else w):return add(origin,'const',1,bits='0',signed=False)
        c=add(origin,'const',w,bits=format(index,f'0{w}b'),signed=a['signed'])
        return add(origin,'case_eq',1,[address,c],signed=False,operand_width=w,compare_signed=a['signed'])
    for n in ir['nodes']:
        op=n['op'];args=[mapped[a] for a in n['args']];w=n['width']
        if op=='memory_ref':
            value={i:add(n,'ref',w,name=s['name']) for i,s in words[n['name']].items()}
        elif op=='memory_read':
            value=add(n,'const',w,bits='x'*w)
            for i,s in words[n['name']].items():
                ref=add(n,'ref',w,name=s['name']);value=add(n,'mux',w,[match(n,args[0],i),ref,value])
        elif op=='memory_write':
            before,address,data=args;value={}
            for i in words[n['name']]:
                candidate=data;positions=n['positions']
                if positions is not None:
                    chunks=[add(n,'slice',1,[data],bits=[len(positions)-1-positions.index(bit)]) if bit in positions else
                        add(n,'slice',1,[before[i]],bits=[bit]) for bit in reversed(range(w))]
                    candidate=add(n,'concat',w,chunks)
                value[i]=add(n,'proc_mux',w,[match(n,address,i),candidate,before[i]])
        elif op=='memory_merge':
            c,a,b=args
            if not isinstance(a,dict) or set(a)!=set(b):raise ValueError('Invalid memory version merge')
            value={i:a[i] if a[i]==b[i] else add(n,'proc_mux',w,[c,a[i],b[i]]) for i in a}
        else:
            if any(isinstance(a,dict) for a in args):raise ValueError('Memory version used as scalar')
            value=add(n,op,w,args)
        mapped[n['id']]=value
    out['nodes']=nodes;out['assignments']={n:mapped[i] for n,i in ir['assignments'].items()};next_state={}
    for name,i in ir['next_state'].items():
        if name in mem:next_state.update({words[name][j]['name']:v for j,v in mapped[i].items()})
        else:next_state[name]=mapped[i]
    out['next_state']=next_state
    for p in out['events']['processes']:
        p['states']=[w['name'] for s in p['states'] for w in (list(words[s].values()) if s in mem else [states[s]])]
    out['events']['expanded_memory_states']=[w['name'] for a in words.values() for w in a.values()]
    for assertion in out.get('assertions',[]):
        for key in ('enable_node','pass_node'):assertion[key]=mapped[assertion[key]]
    out['capabilities']=sorted((set(out['capabilities'])-{'compact_memory'})|({'unpacked_memory'} if mem else set()))
    out['format']='chisel2lean-fourstate-ir-v5'
    out['elaboration']['events']=out['events'];out['elaboration']['states']=out['states'];out['elaboration']['memories']=out.get('memories',[])
    out['private_lowering']=dict(format='fourstate-compact-to-scalar-v1',compact_sha256=signature(ir),selection=list(allowed_tops))
    return out
