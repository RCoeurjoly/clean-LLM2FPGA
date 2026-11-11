"""Private v7 event admission: F46 source semantics without cardinality budgets.

Dependency queries use an iterative memoized graph. This is not admission of
state-derived event sources, NBA event feedback, or arbitrary SV scheduling.
"""
from collections import defaultdict


def combinational_order(signals,nodes,assignments,states,fail,symbols):
    """Memoized ordered symbol dependencies and iterative DFS, no call depth cap."""
    deps=[]
    for n in nodes:
        names=[n['name']] if n['op'] in ('ref','select','memory_read','memory_ref') else []
        for a in n['args']:names.extend(deps[a])
        deps.append(list(dict.fromkeys(names)))
    done=set(states)|{n for n,s in signals.items() if s['direction']=='input'};active=set();order=[]
    for name in assignments:
        if name in done:continue
        active.add(name);stack=[(name,iter(deps[assignments[name]]))]
        while stack:
            current,children=stack[-1]
            child=next(children,None)
            if child is None:
                stack.pop();active.remove(current);done.add(current);order.append(current);continue
            if child in active:fail(symbols[child],'combinational cycle: '+child)
            if child in done:continue
            active.add(child);stack.append((child,iter(deps[assignments[child]])))
    return order


def finish_contract(top, roots, processes, signals, nodes, assignments, next_state,
                    states, dependencies, fail, symbols, assertions=(), reset_aliases=None,
                    *, _memory_names=(), _eventful=False, _scalar_nba_bounds=None, _extensions=False):
    root_paths = {top+'.'+r for r in roots}
    owned = defaultdict(list)
    for s in states: owned[s['process']].append(s['name'])
    latch_names = {s['name'] for s in states if processes[s['process']]['kind']=='latch'}
    def error(name, message): fail(symbols[name], message)
    def strip(i):
        seen=set()
        while True:
            if i in seen: error(nodes[i].get('name',next(iter(signals))), 'scalable dependency cycle')
            seen.add(i); n=nodes[i]
            if n['op'] in ('cast_sign','resize') and n['width']==1: i=n['args'][0]
            elif n['op']=='ref' and n['name'] in assignments: i=assignments[n['name']]
            else:return n
    # Flags: root clock, admitted latch state, other non-input state.
    memo={}; graph_edges=0
    def flags(start):
        nonlocal graph_edges
        stack=[(start,False)];active=set()
        while stack:
            i,done=stack.pop()
            if i in memo:continue
            n=nodes[i];args=list(n['args']);base=0
            if n['op'] in ('ref','select','memory_read','memory_ref'):
                name=n['name']
                if name in assignments:args.append(assignments[name])
                else:base=(1 if name in root_paths else 2 if name in latch_names else
                           0 if signals[name]['direction']=='input' else 4)
            if done:
                value=base
                for a in args:value|=memo[a]
                memo[i]=value;active.remove(i);graph_edges+=len(args)
            else:
                if i in active:error(n.get('name',next(iter(signals))), 'scalable dependency cycle')
                active.add(i);stack.append((i,True))
                for a in reversed(args):
                    if a not in memo:stack.append((a,False))
        return memo[start]
    latches={}
    for p in processes:
        p['states']=owned[p['id']]
        if not p['states'] and (not _extensions or p['kind']=='latch'):error(next(iter(signals)), 'event process without state unsupported')
        if p['kind']!='latch':continue
        name=p['states'][0];n=nodes[next_state[name]]
        if len(p['states'])!=1 or n['op']!='proc_mux':error(name,'one latch target with explicit hold required')
        c=strip(n['args'][0])
        if c['op']!='logical_not' or strip(c['args'][0])['op']!='ref' or strip(c['args'][0])['name'] not in root_paths:
            error(name,'bounded latch guard must be !root_clock')
        if nodes[n['args'][2]]['op']!='ref' or nodes[n['args'][2]]['name']!=name:
            error(name,'latch hold must retain its own state')
        if flags(n['args'][1]) & (3 if _eventful else 7):error(name,'latch data must depend only on nonclock top inputs; feedback/chains unsupported')
        p['root']=strip(c['args'][0])['name'];latches[name]=p['root']
    def clock_expr(n,name):
        if n['op']=='ref' and n['name'] in root_paths:return dict(root=n['name'],form='direct')
        if n['op'] in ('not','logical_not'):
            x=strip(n['args'][0])
            if x['op']=='ref' and x['name'] in root_paths:return dict(root=x['name'],form='inverted')
        if n['op']=='and':
            for a,b in (n['args'],list(reversed(n['args']))):
                x,y=strip(a),strip(b)
                if x['op']==y['op']=='ref' and x['name'] in root_paths and latches.get(y['name'])==x['name']:
                    return dict(root=x['name'],form='latched_and',latch=y['name'])
        error(name,'unsupported derived event source; only direct/inverted roots or low-latched AND clock admitted')
    monitors=sorted(root_paths);monitor_set=set(monitors);reset_roots=set()
    state_by_name={s['name']:s for s in states}
    reset_edges=[]
    def reset_bit(name):
        i=assignments.get(name);n=strip(i) if i is not None else dict(op='ref',name=name)
        inverted=False
        if n['op'] in ('not','logical_not'):
            inverted=True;n=strip(n['args'][0])
        bit=0
        if n['op']=='select' and n['width']==1:
            bit=n['bits'][0];n=dict(op='ref',name=n['name'])
        elif n['op']=='slice' and n['width']==1:
            bit=n['bits'][0];n=strip(n['args'][0])
        elif n['op']=='ref' and signals[n['name']]['width']!=1:
            error(name,'derived reset requires one static state bit')
        if n['op']!='ref' or n['name'] not in state_by_name or n['name'] in _memory_names:
            error(name,'derived reset requires one static scalar state bit; reset logic/glitches unsupported')
        s=state_by_name[n['name']]
        if processes[s['process']]['kind']!='edge':error(name,'latch-derived reset may race with active settling')
        if (_scalar_nba_bounds or {}).get(s['name'])!=1:
            error(name,'derived reset producer requires at most one executed NBA write per activation; intermediate NBA edges unsupported')
        return dict(form='state_bit_reset',state=s['name'],bit=bit,inverted=inverted,producer=s['process'],producer_nba_bound=1,source=s['source'])
    def reset_source(name):
        path,links=[name],[];seen={name}
        while signals[name]['direction']!='input':
            edge=(reset_aliases or {}).get(name)
            if edge is None:error(name,'async reset requires a unique one-bit identity alias to a top input')
            links.append(dict(signal=name,**edge));name=edge['source']
            if name in seen:error(name,'async reset alias cycle')
            seen.add(name);path.append(name)
        if signals[name]['width']!=1 or name in root_paths:error(name,'async reset must have a distinct one-bit top input root')
        reset_roots.add(name);return name,path,links
    for p in processes:
        for t in p['triggers']:
            name=t['signal']
            if name==p.get('reset'):
                probe=name;seen=set()
                while probe not in seen and signals[probe]['direction']!='input' and probe in (reset_aliases or {}):
                    seen.add(probe);probe=reset_aliases[probe]['source']
                if _eventful and signals[probe]['direction']!='input':
                    t['source']=reset_bit(name);reset_edges.append((t['source']['producer'],p['id']))
                else:
                    root,path,links=reset_source(name);t['source']=dict(form='reset')
                    if name!=root:
                        t['original_signal']=name;t['signal']=root;t['source'].update(root=root,alias_path=path,alias_links=links);name=root
            else:t['source']=clock_expr(strip(assignments[name]) if name in assignments else dict(op='ref',name=name),name)
            if name not in monitor_set:monitors.append(name);monitor_set.add(name)
        if p['kind']=='edge':
            for name in p['states']:
                if flags(next_state[name])&3:error(name,'edge data reading roots/latches may race with active settling; unsupported')
    for a in assertions:
        dependencies_flags=flags(a['enable_node'])|flags(a['pass_node'])
        for arg in a.get('arguments',[]):dependencies_flags|=flags(arg['node'])
        if dependencies_flags&3:
            error(monitors[0],'assertion reading roots/latches may race with active settling; unsupported')
    # Current admitted latch data is input-only: one update layer followed by
    # one equality check is a structural bound, independent of latch count.
    layers=1 if latches else 0
    result=dict(format='fourstate-scalable-events-v1',roots=list(roots),processes=processes,monitors=monitors,
        max_delta=layers+1,max_activations_per_process=1,reset_roots=sorted(reset_roots),
        dependency_stats=dict(memo_nodes=len(memo),edges=graph_edges,latch_layers=layers),
        input_contract='one original input port changes per ordered step',
        initial_contract='explicit original state; stable seed; time-zero excluded',
        sampling='pre=active settled before NBA; post=after simultaneous NBA and settling',
        conflict_contract='single writer; no state-derived event sources or NBA event feedback',
        memory_objects=list(_memory_names),scalar_state_bits=sum(s['width'] for s in states if s['name'] not in _memory_names))
    if _eventful:
        from fourstate_eventful import dependency_plan
        graph=dependency_plan(processes,reset_edges,lambda pid,msg:error(processes[pid]['states'][0],msg))
        result.update(format='fourstate-eventful-events-v1',reset_dependency_edges=[list(e) for e in sorted(set(reset_edges))],
            reset_topology=graph['order'],max_waves=graph['depth']+2,
            wave_bound_reason='Acyclic static-bit reset NBA paths plus one state-driven latch settlement wave',
            conflict_contract='No active reset logic/glitches, clock/latch data races, reset cycles or NBA changes of a clock source',
            max_activations_per_process=graph['depth']+2)
    return result,monitors


def plan_events(ir):
    e=ir['events'];watchers=[[] for _ in e['monitors']];ids={n:i for i,n in enumerate(e['monitors'])}
    for p in e['processes']:
        for t in p['triggers']:watchers[ids[t['signal']]].append(dict(process=p['id'],edge=t['edge']))
    result=dict(format='fourstate-event-plan-v1',monitors=list(e['monitors']),watchers=watchers,
        owners=[dict(process=p['id'],states=list(p['states'])) for p in e['processes']],
        latch_layers=[[p['id'] for p in e['processes'] if p['kind']=='latch']] if e['dependency_stats']['latch_layers'] else [],
        settle_checks=e['max_delta'],phases=['capture','early','latch','late','pre','commit','post'])
    if e['format']=='fourstate-eventful-events-v1':
        result.update(format='fourstate-event-plan-v2',max_waves=e['max_waves'],reset_dependency_edges=e['reset_dependency_edges'],
            phases=['capture','early','latch','late','wave_pre_and_commit','check_and_repeat_or_post','post'])
    return result
