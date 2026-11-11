"""F4.6 private, explicitly bounded event contract; no general SV scheduler.

Admission permits low-transparent single-assignment latches, alias/inverted
roots and ClockGate-shaped clocks. Each trace step changes at most one input.
Independent NBA processes read the same old state and commit simultaneously.
"""
from __future__ import annotations

ALPHABET = '01xz'
POSEDGE = {('0', '1'), ('0', 'x'), ('0', 'z'), ('x', '1'), ('z', '1')}
NEGEDGE = {(b, a) for a, b in POSEDGE}


def import_process(proc, reference, kind, fail, owner, audit, *, _extensions=False):
    body = proc.body
    latch = str(proc.procedureKind).split('.')[-1] == 'AlwaysLatch'
    def unwrap(n):
        while kind(n) == 'Block':
            if str(n.blockKind).split('.')[-1] != 'Sequential':
                fail(n, 'sequential bounded-event block required')
            n = n.body
        if kind(n) == 'List' and len(n.list) == 1:
            return unwrap(n.list[0])
        return n
    if latch:
        n = unwrap(body)
        if (kind(n) != 'Conditional' or len(n.conditions) != 1 or n.ifFalse is not None or
                n.conditions[0].pattern is not None or 'None' not in str(n.check)):
            fail(proc, 'bounded latch requires if (!root_clock) single blocking assignment')
        a = unwrap(n.ifTrue)
        if kind(a) != 'ExpressionStatement' or kind(a.expr) != 'Assignment' or a.expr.isNonBlocking:
            fail(proc, 'bounded latch requires one whole blocking assignment')
        return dict(kind='latch', triggers=[]), body
    if kind(body) != 'Timed':
        fail(proc, 'bounded edge process requires explicit event control')
    events = list(body.timing.events) if kind(body.timing) == 'EventList' else [body.timing]
    if not 1 <= len(events) <= 2:
        fail(proc, 'one clock and at most one reset event required')
    triggers = []
    for e in events:
        if (kind(e) != 'SignalEvent' or str(e.edge).split('.')[-1] not in ('PosEdge', 'NegEdge') or
                e.iffCondition is not None or reference(e.expr) is None or e.expr.type.bitWidth != 1):
            fail(proc, 'named one-bit posedge/negedge without iff required')
        audit(e.expr, owner, 'bounded event')
        triggers.append(dict(signal=reference(e.expr), edge=str(e.edge).split('.')[-1]))
    reset = None
    if len(events) == 2:
        n = unwrap(body.stmt)
        if _extensions and kind(n)=='List':
            rest=[item for item in n.list if kind(item) not in ('VariableDeclaration','Empty')]
            n=unwrap(rest[0]) if rest else n
        if kind(n) != 'Conditional' or len(n.conditions) != 1 or n.conditions[0].pattern is not None:
            fail(proc, 'two-event process must begin with if (reset)')
        reset = reference(n.conditions[0].expr)
        if reset is None or not any(t == dict(signal=reset, edge='PosEdge') for t in triggers):
            fail(proc, 'direct positive reset condition and event required')
    return dict(kind='edge', triggers=triggers, reset=reset), body.stmt


def finish_contract(top, roots, processes, signals, nodes, assignments, next_state, states, dependencies, fail, symbols, assertions=(), reset_aliases=None, *, _memory_names=()):
    if len(processes) > 8 or sum(s['width'] for s in states if s['name'] not in _memory_names) > 256:
        fail(symbols[states[0]['name']], 'bounded event budget: 8 processes and 256 state bits')
    root_paths = {top + '.' + r for r in roots}
    def error(name, msg):
        fail(symbols[name], msg)
    def strip(i):
        n = nodes[i]
        if n['op'] in ('cast_sign', 'resize') and n['width'] == 1:
            return strip(n['args'][0])
        if n['op'] == 'ref' and n['name'] in assignments:
            return strip(assignments[n['name']])
        return n
    def leaves(i, active=()):
        result = set()
        for name in dependencies(i):
            if name in active:
                error(name, 'bounded event dependency cycle')
            if name in assignments:
                result |= leaves(assignments[name], (*active, name))
            else:
                result.add(name)
        return result
    latches = {}
    for p in processes:
        p['states'] = [s['name'] for s in states if s['process'] == p['id']]
        if not p['states']:
            error(next(iter(signals)), 'event process without state unsupported')
        if p['kind'] != 'latch':
            continue
        if len(p['states']) != 1:
            error(p['states'][0], 'one latch target required')
        name = p['states'][0];n = nodes[next_state[name]]
        if n['op'] != 'proc_mux':
            error(name, 'latch must have an explicit hold branch')
        c = strip(n['args'][0])
        if c['op'] != 'logical_not' or strip(c['args'][0])['op'] != 'ref' or strip(c['args'][0])['name'] not in root_paths:
            error(name, 'bounded latch guard must be !root_clock')
        if nodes[n['args'][2]]['op'] != 'ref' or nodes[n['args'][2]]['name'] != name:
            error(name, 'latch hold must retain its own state')
        if any(signals[d]['direction'] != 'input' or d in root_paths for d in leaves(n['args'][1])):
            error(name, 'latch data must depend only on nonclock top inputs; feedback/chains unsupported')
        p['root'] = strip(c['args'][0])['name'];latches[name] = p['root']
    def clock_expr(n, name):
        if n['op'] == 'ref' and n['name'] in root_paths:
            return dict(root=n['name'], form='direct')
        if n['op'] in ('not', 'logical_not'):
            v = strip(n['args'][0])
            if v['op'] == 'ref' and v['name'] in root_paths:
                return dict(root=v['name'], form='inverted')
        if n['op'] == 'and':
            for a, b in (n['args'], list(reversed(n['args']))):
                x, y = strip(a), strip(b)
                if x['op'] == y['op'] == 'ref' and x['name'] in root_paths and latches.get(y['name']) == x['name']:
                    return dict(root=x['name'], form='latched_and', latch=y['name'])
        error(name, 'unsupported derived event source; only direct/inverted roots or low-latched AND clock admitted')
    monitors = sorted(root_paths)
    reset_roots = set()
    def reset_source(name):
        path, links = [name], []
        while signals[name]['direction'] != 'input':
            edge = (reset_aliases or {}).get(name)
            if edge is None:
                error(name, 'async reset requires a unique one-bit identity alias to a top input')
            links.append(dict(signal=name, **edge))
            name = edge['source']
            if name in path:
                error(name, 'async reset alias cycle')
            path.append(name)
        if signals[name]['width'] != 1 or name in root_paths:
            error(name, 'async reset must have a distinct one-bit top input root')
        reset_roots.add(name)
        if len(reset_roots) > 1:
            error(name, 'bounded event budget: one independent async reset root')
        return name, path, links
    for p in processes:
        for t in p['triggers']:
            name = t['signal']
            if name == p.get('reset'):
                root, path, links = reset_source(name)
                t['source'] = dict(form='reset')
                if name != root:
                    t['original_signal'] = name
                    t['signal'] = root
                    t['source'].update(root=root, alias_path=path, alias_links=links)
                    name = root
            else:
                n = strip(assignments[name]) if name in assignments else dict(op='ref', name=name)
                t['source'] = clock_expr(n, name)
            if name not in monitors:
                monitors.append(name)
        if p['kind'] == 'edge':
            for name in p['states']:
                used = leaves(next_state[name])
                if used & (root_paths | set(latches)):
                    error(name, 'edge data reading roots/latches may race with active settling; unsupported')
    if len(monitors) > 16:
        error(monitors[0], 'bounded monitor budget exceeded')
    for a in assertions:
        if (leaves(a['enable_node']) | leaves(a['pass_node'])) & (root_paths | set(latches)):
            error(monitors[0], 'assertion reading roots/latches may race with active settling; unsupported')
    result = dict(format='fourstate-bounded-events-v1', roots=list(roots), processes=processes,
                  monitors=monitors, max_delta=8, max_activations_per_process=1,
                  input_contract='at most one original input changes per trace step; explicit ordered steps',
                  initial_contract='stable history seed, explicit all-state deposit, latch state must be consistent; time-zero execution excluded',
                  sampling='pre=active/latch settled before NBA; post=after simultaneous NBA and settling',
                  conflict_contract='one writer per state; no memory, state-derived clocks except admitted latches, feedback or arbitrary combinational clocks')
    if _memory_names:
        result.update(composition='fourstate-compact-memory-events-v1',
            conflict_contract='one writer per scalar/memory; edge-only memory NBA; no asynchronous memory reset; original bounded event scheduling',
            scalar_state_bits=sum(s['width'] for s in states if s['name'] not in _memory_names),
            memory_objects=list(_memory_names))
    return result, monitors


def validate_contract(mapping):
    e = mapping.get('events', {})
    if e.get('format') != 'fourstate-bounded-events-v1' or e.get('max_delta') != 8:
        raise ValueError('Invalid bounded event contract')
    if not 1 <= len(e.get('roots', [])) <= 2 or not e.get('processes') or len(e['processes']) > 8:
        raise ValueError('Invalid bounded event roots/processes')
    if any(p.get('role') == 'clock' for p in mapping['ports']):
        raise ValueError('Bounded event clocks require full four-state rails')
    owned = [name for p in e['processes'] for name in p['states']]
    if len(owned) != len(set(owned)) or set(owned) != {s['name'] for s in mapping['states']}:
        raise ValueError('Bounded event ownership mismatch')
    if [m['name'] for m in mapping.get('monitors', [])] != e['monitors']:
        raise ValueError('Bounded event monitor mismatch')
    roots=e['roots'];ports={p['name']:p for p in mapping['ports']}
    if len(set(roots))!=len(roots) or e.get('max_activations_per_process')!=1:
        raise ValueError('Invalid bounded event limits')
    if any(r not in ports or ports[r]['direction']!='input' or ports[r]['width']!=1 for r in roots):
        raise ValueError('Invalid bounded event root interface')
    if len(e['monitors'])!=len(set(e['monitors'])) or any(m['width']!=1 for m in mapping['monitors']):
        raise ValueError('Invalid bounded monitor inventory')
    for i,p in enumerate(e['processes']):
        if p['id']!=i or p['kind'] not in ('latch','edge') or not p['states']:
            raise ValueError('Invalid bounded process inventory')
        if any(s['process']!=i for s in mapping['states'] if s['name'] in p['states']):
            raise ValueError('Bounded state process ownership mismatch')
        if p['kind']=='edge' and (not 1<=len(p['triggers'])<=2 or any(t['signal'] not in e['monitors'] or t['edge'] not in ('PosEdge','NegEdge') for t in p['triggers'])):
            raise ValueError('Invalid bounded trigger inventory')
        if p['kind']=='latch' and (p['triggers'] or len(p['states'])!=1 or p['root'] not in {mapping['top']+'.'+r for r in roots}):
            raise ValueError('Invalid bounded latch inventory')
        for t in p['triggers']:
            if 'original_signal' not in t:
                continue
            src=t.get('source',{});path=src.get('alias_path',[]);links=src.get('alias_links',[])
            root=t['signal'];prefix=mapping['top']+'.'
            port=ports.get(root[len(prefix):] if root.startswith(prefix) else '',{})
            if (src.get('form')!='reset' or src.get('root')!=root or
                    len(path)<2 or path[0]!=t['original_signal'] or path[-1]!=root or
                    len(path)!=len(set(path)) or len(links)!=len(path)-1 or
                    port.get('direction')!='input' or port.get('width')!=1 or
                    root in {mapping['top']+'.'+r for r in roots} or
                    any(link.get('signal')!=a or link.get('source')!=b or
                        link.get('connection') not in ('input_port','local_net')
                        for a,b,link in zip(path,path[1:],links))):
                raise ValueError('Invalid bounded reset alias provenance')


def validate_trace(mapping, trace, initial):
    validate_contract(mapping)
    names = {p['name'] for p in mapping['ports'] if p['direction'] == 'input'}
    if set(initial) != {s['name'] for s in mapping['states']} or not trace:
        raise ValueError('Nonempty event trace and exactly all original initial states required')
    previous = None
    for row in trace:
        if set(row) != {'inputs'} or set(row['inputs']) != names:
            raise ValueError('Supply exactly all original inputs')
        if previous is not None and sum(previous[n] != row['inputs'][n] for n in names) > 1:
            raise ValueError('Simultaneous input changes/races outside bounded event contract')
        previous = row['inputs']
    rows = rail_encode_trace(mapping, trace)
    ports = [dict(s, direction='input', tag=s['current_tag'], value=s['current_value']) for s in mapping['states']]
    init = rail_encode_trace({'ports': ports}, [{'inputs': initial}])[0]['inputs']
    return rows, init


def rail_encode_trace(mapping, trace):
    """Stdlib-only transport adapter, also usable by Python 3.8 server benches."""
    result=[]
    ports=[p for p in mapping['ports'] if p['direction']=='input']
    for row in trace:
        values={}
        if set(row['inputs'])!={p['name'] for p in ports}:raise ValueError('Exact input set required')
        for p in ports:
            v=row['inputs'][p['name']]
            if not isinstance(v,str) or len(v)!=p['width'] or set(v)-set(ALPHABET):raise ValueError('Exact 01xz width required')
            values[p['tag']]=int(''.join('1' if c in 'xz' else '0' for c in v),2)
            values[p['value']]=int(''.join('1' if c in '1z' else '0' for c in v),2)
        result.append(dict(inputs=values))
    return result


def rail_decode(ports,values,state=False):
    result={};keys=set()
    for p in ports:
        tk,vk=('current_tag','current_value') if state else ('tag','value')
        t,v=values[p[tk]],values[p[vk]];keys.update((p[tk],p[vk]))
        if any(type(n) is not int or not 0<=n<1<<p['width'] for n in (t,v)):raise ValueError('Invalid rail value')
        result[p['name']]=''.join(ALPHABET[(((t>>i)&1)<<1)|((v>>i)&1)] for i in reversed(range(p['width'])))
    if set(values)!=keys:raise ValueError('Unexpected rail set')
    return result


def runner_source(ir, mapping):
    import json
    from lean_records import access_paths
    from fourstate_sequential import runner_source as old_source
    prefix = old_source(ir, mapping).split('\ndef main (args : List String)', 1)[0]
    paths = access_paths((p['field'] for p in ir['outputs']), ir.get('lean_record_group_size', 128))
    outputs = {p['name']: 'o.' + paths[p['field']] + '.toNat' for p in ir['outputs']}
    monitor = '#[' + ', '.join(f'({outputs[m["tag"]]} * 2 + {outputs[m["value"]]})' for m in mapping['monitors']) + ']'
    # Explicit per-state ownership; candidates already implement statement/NBA
    # order in the existing converter, the wrapper only selects activations.
    pairs = []
    for s in mapping['states']:
        for k in ('current_tag', 'current_value'):
            key = json.dumps(s[k])
            pairs.append(f'({key}, ← (if ids.contains {s["process"]} then next else regs).getObjVal? {key})')
    processes = mapping['events']['processes']
    latch_ids = '#[' + ','.join(str(p['id']) for p in processes if p['kind'] == 'latch') + ']'
    fired = []
    for p in processes:
        if p['kind'] != 'edge':
            continue
        checks = []
        for t in p['triggers']:
            i = mapping['events']['monitors'].index(t['signal'])
            checks.append(f'{"posedge4" if t["edge"] == "PosEdge" else "negedge4"} old[{i}]! now[{i}]!')
        fired.append(f'  if {" || ".join(checks)} then ids := ids.push {p["id"]}')
    source = prefix + f'''
def monitors (o : Outputs) : Array Nat := {monitor}
def selectStates (regs next : Lean.Json) (ids : Array Nat) : Except String Lean.Json := do
  return Lean.Json.mkObj [{', '.join(pairs)}]
def posedge4 (a b : Nat) : Bool := (a == 0 && b != 0) || ((a == 2 || a == 3) && b == 1)
def negedge4 (a b : Nat) : Bool := posedge4 b a
def triggered (old now : Array Nat) : Array Nat := Id.run do
  let mut ids : Array Nat := #[]
{chr(10).join(fired)}
  return ids
def observed (o : Outputs) (regs : Lean.Json) : Lean.Json :=
  Lean.Json.mkObj [("outputs", encodeOutputs o), ("states", regs), ("monitors", Lean.toJson (monitors o))]
def eventStep (row : Lean.Json) (initial : Lean.Json) (previous : Array Nat) (sample : Nat) :
    IO (Lean.Json × Array Nat × Lean.Json) := do
    let mut regs := initial
    let j ← unwrap (row.getObjVal? "inputs")
    let early ← checked (← unwrap (decodeInputs j regs))
    let mut ids := if sample == 0 then #[] else triggered previous (monitors early)
    let mut stable := false
    for _ in [:8] do
      if !stable then
        let o ← checked (← unwrap (decodeInputs j regs))
        let changed ← unwrap (selectStates regs (encodeNext o) {latch_ids})
        stable := changed.compress == regs.compress
        if sample == 0 && !stable then
          throw (IO.userError "Initial latch state is inconsistent with history seed")
        let after ← checked (← unwrap (decodeInputs j changed))
        if sample != 0 then
          for id in triggered (monitors o) (monitors after) do
            if ids.contains id then throw (IO.userError "Repeated process event in one input step")
            ids := ids.push id
        regs := changed
    if !stable then throw (IO.userError "Bounded delta settlement did not converge")
    let before ← checked (← unwrap (decodeInputs j regs))
    let pre := observed before regs
    regs ← unwrap (selectStates regs (encodeNext before) ids)
    let after ← checked (← unwrap (decodeInputs j regs))
    if monitors after != monitors before then
      throw (IO.userError "NBA changed event source outside supported contract")
    let post := observed after regs
    return (regs, monitors after, Lean.Json.mkObj [("sample", Lean.toJson sample), ("fired", Lean.toJson ids),
      ("pre", pre), ("post", post)])
def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply request path")
  let request ← unwrap (Lean.Json.parse (← IO.FS.readFile path))
  let rows ← unwrap (request.getObjValAs? (Array Lean.Json) "trace")
  let mut regs ← unwrap (request.getObjVal? "initial")
  let mut previous : Array Nat := #[]
  let mut sample := 0
  for row in rows do
    let (next, history, result) ← eventStep row regs previous sample
    IO.println result.compress
    regs := next
    previous := history
    sample := sample + 1
'''
    if mapping.get('assertions'):
        from fourstate_assertions import adapt_runner
        source = adapt_runner(source,ir,mapping)
    return source


def decode_results(mapping, raw):
    results = []
    for i, row in enumerate(raw):
        if row['sample'] != i:
            raise ValueError('Invalid event sample index')
        r = dict(sample=i, fired=row['fired'])
        if mapping.get('assertions'):
            if not isinstance(row.get('assertions'),list):raise ValueError('Missing Lean assertion observation channel')
            r['assertions']=row['assertions']
        for phase in ('pre','post'):
            p = row[phase]
            if len(p['monitors']) != len(mapping['monitors']) or any(type(n) is not int or not 0 <= n < 4 for n in p['monitors']):
                raise ValueError('Invalid monitor rails')
            r[phase] = dict(outputs=rail_decode([x for x in mapping['ports'] if x['direction']=='output'],p['outputs']),states=rail_decode(mapping['states'],p['states'],True),
                            monitors={m['name']: ALPHABET[n] for m,n in zip(mapping['monitors'],p['monitors'])})
        results.append(r)
    return results


def simulate(mapping, ir_path, trace, initial, evidence):
    import json, os
    from pathlib import Path
    from common import lean_executable, run_logged
    from fourstate2lean import fresh_directory, write
    rows, init = validate_trace(mapping,trace,initial)
    evidence = fresh_directory(evidence)
    write(evidence/'request.json',dict(trace=rows,initial=init))
    ir = json.loads(Path(ir_path).read_text());root = Path(ir_path)
    for _ in ir['namespace'].split('.'):root = root.parent
    src = evidence/'RunEvents.lean';src.write_text(runner_source(ir,mapping),encoding='utf8')
    env = dict(os.environ,LEAN_PATH=str(root))
    if run_logged([lean_executable(),'-j','2','-s','65536','--run',str(src),str(evidence/'request.json')],evidence/'execute',cwd=root,env=env,echo=False):
        raise RuntimeError('Bounded event Lean execution failed: '+str(evidence))
    raw = [json.loads(line) for line in (evidence/'execute/stdout.log').read_text().splitlines() if line.strip()]
    if len(raw) != len(trace):raise ValueError('Incomplete event trace')
    result = decode_results(mapping,raw);write(evidence/'result.json',result)
    return result
