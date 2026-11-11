"""Explicit private v8: acyclic static-bit derived resets and state-fed gates.

Not a general SV scheduler. Native ClockGate low-latch structure isolates its
enable updates from its clock edge; reset NBA propagation uses graph bounds.
"""
from collections import deque


def dependency_plan(processes,edges,fail):
    children=[set() for _ in processes];degree=[0]*len(processes);depth=[0]*len(processes)
    for a,b in set(map(tuple,edges)):
        if a==b:fail(b,'self-triggered derived reset cycle unsupported')
        if b not in children[a]:children[a].add(b);degree[b]+=1
    ready=deque(i for i,n in enumerate(degree) if not n);order=[]
    while ready:
        a=ready.popleft();order.append(a)
        for b in sorted(children[a]):
            depth[b]=max(depth[b],depth[a]+1);degree[b]-=1
            if not degree[b]:ready.append(b)
    if len(order)!=len(processes):fail(next(i for i,n in enumerate(degree) if n),'cyclic derived-reset dependency unsupported')
    return dict(order=order,depth=max(depth,default=0))


def parse_eventful(source,top,clock,*,event_clocks=(),immediate_assertions=False):
    from fourstate_scalable import parse_scalable
    ir=parse_scalable(source,top,clock,event_clocks=event_clocks,immediate_assertions=immediate_assertions,_eventful=True)
    validate_eventful(ir)
    return ir


def validate_eventful(ir):
    from fourstate_scalable import validate_scalable
    validate_scalable(ir,_eventful=True)
    e=ir['events']
    if e['format']!='fourstate-eventful-events-v1':raise ValueError('Expected eventful contract')
    def fail(pid,msg):raise ValueError(str(e['processes'][pid]['source'])+': '+msg)
    graph=dependency_plan(e['processes'],e['reset_dependency_edges'],fail)
    for p in e['processes']:
        for t in p['triggers']:
            s=t['source']
            if s['form']=='state_bit_reset' and (s.get('producer_nba_bound')!=1 or ir['scalar_nba_bounds'].get(s['state'])!=1):
                fail(p['id'],'unsafe intermediate NBA reset source')
    if graph['order']!=e['reset_topology'] or e['max_waves']!=graph['depth']+2:raise ValueError('Derived reset graph/bound mismatch')
    if ir.get('assertions'):
        p=ir['assertions'][0]['source']
        raise ValueError(f'{p["file"]}:{p["line"]}:{p["column"]}: v8 per-wave assertion diagnostics require separate admission; use existing profiles where applicable')


def encode_eventful(ir):
    from fourstate_scalable import encode_scalable
    validate_eventful(ir)
    return encode_scalable(ir,_eventful=True)


def controller(ir,inputs,changed,fire):
    processes=ir['events']['processes'];mon=ir['events']['monitors'];N=ir['events']['max_waves'];M=len(mon)
    edges=[p for p in processes if p['kind']=='edge']
    pending=' || '.join(fire(p,'history') for p in edges) or "1'b0"
    repeat=' || '.join(f'(fired[{p["id"]}] && {fire(p,"early")})' for p in edges) or "1'b0"
    clocks={ir['top']+'.'+r for r in ir['events']['roots']}
    clocks.update(t['signal'] for p in edges for t in p['triggers'] if t['source']['form'] not in ('reset','state_bit_reset'))
    bad_clock=' || '.join(f'(observed_monitors[{2*mon.index(n)} +: 2]!=before_nba[{2*mon.index(n)} +: 2])' for n in sorted(clocks))
    changes=' + '.join(f'{{31\'b0,((in{j}_tag!={p["tag"]})||(in{j}_value!={p["value"]}))}}' for j,p in enumerate(inputs)) or '0'
    result=[
        'assign step_ready=(phase==0 && fault==0); assign pre_valid=(phase==4 && wave_id==0 && fault==0); assign post_valid=(phase==6 && fault==0);',
        'assign wave_valid=(phase==4 && fault==0);',
        f'wire bad_inputs=seeded && (({changes})>1); wire accept=step_ready && step_valid && !bad_inputs;',
        f'wire bad_latch=({changed}); wire bad_initial=!seeded && bad_latch;',
        f'wire bad_repeat=seeded && ({repeat}); wire bad_clock=({bad_clock});',
        f'wire more_work=({pending}) || bad_latch; wire too_many=(wave_id=={N-1}) && more_work;',
        'wire [3:0] fault_next=(phase==0 && step_valid && bad_inputs)?1:(phase==2 && bad_initial)?2:(phase==3 && bad_latch)?3:(phase==3 && bad_repeat)?4:(phase==5 && bad_clock)?5:(phase==5 && too_many)?7:(phase==7)?6:0;',
        'wire [2:0] phase_next=(phase==0)?(step_valid?(bad_inputs?7:1):0):(phase==1)?2:(phase==2)?(bad_initial?7:3):(phase==3)?((bad_latch||bad_repeat)?7:4):(phase==4)?5:(phase==5)?((bad_clock||too_many)?7:(more_work?1:6)):(phase==6)?0:7;',
        'always @(posedge exec_clk) begin',
        'fault<=fault!=0?fault:fault_next;phase<=fault!=0?phase:phase_next;',
        'seeded<=((fault==0)&&(phase==6))?1:seeded;',
        'wave_id<=(fault!=0)?wave_id:accept?0:(phase==5 && more_work && !too_many && !bad_clock)?wave_id+1:wave_id;',
        'early<=((fault==0)&&(phase==1))?observed_monitors:early;',
        'before_nba<=((fault==0)&&(phase==3))?observed_monitors:before_nba;',
        'history<=((fault==0)&&((phase==3)||(phase==6)))?observed_monitors:history;']
    for j,p in enumerate(inputs):
        for key in ('tag','value'):result.append(f'in{j}_{key}<=accept?{p[key]}:in{j}_{key};')
    for p in processes:
        pid=p['id']
        result.append(f'fired[{pid}]<=0;' if p['kind']=='latch' else f'fired[{pid}]<=(fault!=0)?fired[{pid}]:(phase==0 && accept)?0:(phase==1)?(seeded && {fire(p,"history")}):(phase==3)?(fired[{pid}] || (seeded && {fire(p,"early")})):fired[{pid}];')
    return result+['end']
