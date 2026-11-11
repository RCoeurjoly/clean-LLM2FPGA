"""F4.3: a bounded sampled wrapper for one posedge clock/reset process.

The existing Lean model computes candidate NBA values. This wrapper performs
event detection and one simultaneous commit in actual Lean execution. It does
not simulate SystemVerilog scheduling or time-zero initial processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ALPHABET = '01xz'
POSEDGE = {('0', '1'), ('0', 'x'), ('0', 'z'), ('x', '1'), ('z', '1')}


def contract(clock, reset):
    return dict(format='fourstate-async-posedge-v1', clock=clock, reset=reset,
                edge='posedge', initial_controls='00',
                simultaneous='one atomic clock/reset vector update; one process activation',
                sampling='data stable before an event; pre=after input settling before NBA; post=after NBA settling',
                time_zero='excluded; controls settle to 00, then explicit state deposit before sampling',
                process_count=1)


def validate_contract(mapping):
    info = mapping.get('events')
    if not isinstance(info, dict) or not isinstance(info.get('reset'), str):
        raise ValueError('Invalid asynchronous event contract')
    if (info != contract(mapping.get('clock'), info['reset']) or info['reset'] == mapping.get('clock') or
            mapping.get('mode') != 'sequential' or not mapping.get('states')):
        raise ValueError('Invalid asynchronous event contract')
    ports = {p['name']: p for p in mapping['ports']}
    for name in (info['clock'], info['reset']):
        if name not in ports or ports[name]['direction'] != 'input' or ports[name]['width'] != 1:
            raise ValueError('Asynchronous event controls must be one-bit inputs')
    if ports[info['clock']].get('role') != 'clock' or 'tag' not in ports[info['reset']]:
        raise ValueError('Invalid asynchronous clock/reset encoding')


def validate(mapping, trace, initial):
    from fourstate_simulate import encode_trace
    validate_contract(mapping)
    states = mapping['states']
    if not isinstance(initial, dict) or set(initial) != {s['name'] for s in states}:
        raise ValueError('Supply exactly every original register in --initial-file')
    init_ports = [dict(p, direction='input', tag=p['current_tag'], value=p['current_value']) for p in states]
    encoded_initial = encode_trace({'ports': init_ports}, [{'inputs': initial}])[0]['inputs']
    clock, reset = mapping['clock'], mapping['events']['reset']
    inputs = [p for p in mapping['ports'] if p['direction'] == 'input']
    names = {p['name'] for p in inputs}
    if not isinstance(trace, list) or not trace:
        raise ValueError('Nonempty asynchronous event trace required')
    previous, rows = None, []
    for i, row in enumerate(trace):
        if not isinstance(row, dict) or set(row) != {'inputs'} or not isinstance(row['inputs'], dict) or set(row['inputs']) != names:
            raise ValueError('Trace must supply exactly all original inputs')
        values = row['inputs']
        for p in inputs:
            value = values[p['name']]
            if not isinstance(value, str) or len(value) != p['width'] or any(c not in ALPHABET for c in value):
                raise ValueError('Exact-width lowercase 01xz input required: ' + p['name'])
        if i == 0 and (values[clock], values[reset]) != ('0', '0'):
            raise ValueError('First asynchronous sample must have clock=reset=0; time-zero events unsupported')
        if previous is not None and ((previous[clock], values[clock]) in POSEDGE or (previous[reset], values[reset]) in POSEDGE):
            if any(values[n] != previous[n] for n in names - {clock, reset}):
                raise ValueError('Data must be stable before clock/reset events; simultaneous data races unsupported')
        ports = [p for p in inputs if p.get('role') != 'clock']
        data = {n: v for n, v in values.items() if n != clock}
        encoded = encode_trace({'ports': ports}, [{'inputs': data}])[0]
        rows.append(dict(clock=ALPHABET.index(values[clock]), reset=ALPHABET.index(values[reset]), **encoded))
        previous = values
    return rows, encoded_initial


def runner_source(ir, mapping):
    from fourstate_sequential import runner_source as synchronous_source
    # Reuse decoding, candidate evaluation and output/state encoding verbatim.
    prefix = synchronous_source(ir, mapping).split('\ndef main (args : List String)', 1)[0]
    return prefix + '''
def posedge4 (old value : Nat) : Bool :=
  (old == 0 && value != 0) || ((old == 2 || old == 3) && value == 1)

def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply request path")
  let request ← unwrap (Lean.Json.parse (← IO.FS.readFile path))
  let rows ← unwrap (request.getObjValAs? (Array Lean.Json) "trace")
  let mut regs ← unwrap (request.getObjVal? "initial")
  let mut previousClock : Nat := 0
  let mut previousReset : Nat := 0
  let mut sample : Nat := 0
  for row in rows do
    let clock ← unwrap (row.getObjValAs? Nat "clock")
    let reset ← unwrap (row.getObjValAs? Nat "reset")
    if clock > 3 || reset > 3 || (sample == 0 && (clock != 0 || reset != 0)) then
      throw (IO.userError "Invalid bounded asynchronous controls")
    let j ← unwrap (row.getObjVal? "inputs")
    let before ← checked (← unwrap (decodeInputs j regs))
    let pre := snapshot (encodeOutputs before) regs
    let clockRising := posedge4 previousClock clock
    let resetRising := posedge4 previousReset reset
    let fired := clockRising || resetRising
    if fired then
      regs := encodeNext before
    let after ← checked (← unwrap (decodeInputs j regs))
    let post := snapshot (encodeOutputs after) regs
    IO.println ((Lean.Json.mkObj [("sample", Lean.toJson sample),
      ("clock", Lean.toJson clock), ("reset", Lean.toJson reset),
      ("clock_rising", Lean.toJson clockRising), ("reset_rising", Lean.toJson resetRising),
      ("fired", Lean.toJson fired), ("pre", pre), ("post", post)]).compress)
    previousClock := clock
    previousReset := reset
    sample := sample + 1
'''


def simulate(mapping, ir_path, trace, initial, evidence):
    from common import lean_executable, run_logged
    from fourstate2lean import fresh_directory, write
    from fourstate_simulate import decode
    from fourstate_sequential import state_ports
    encoded, encoded_initial = validate(mapping, trace, initial)
    evidence = fresh_directory(evidence)
    write(evidence / 'fourstate-request.json', dict(trace=trace, initial=initial, contract=mapping['events']))
    write(evidence / 'request.json', dict(trace=encoded, initial=encoded_initial))
    ir = json.loads(Path(ir_path).read_text(encoding='utf-8'))
    source = evidence / 'RunAsync.lean'
    source.write_text(runner_source(ir, mapping), encoding='utf-8')
    root = Path(ir_path)
    for _ in ir['namespace'].split('.'):
        root = root.parent
    env = os.environ.copy(); env['LEAN_PATH'] = str(root)
    if run_logged([lean_executable(), '--run', str(source), str(evidence / 'request.json')],
                  evidence / 'lean-execution', cwd=root, env=env, echo=False):
        raise RuntimeError('Asynchronous Lean execution failed; see ' + str(evidence / 'lean-execution'))
    raw = [json.loads(line) for line in (evidence / 'lean-execution/stdout.log').read_text().splitlines() if line.strip()]
    write(evidence / 'encoded-result.json', raw)
    if len(raw) != len(trace):
        raise ValueError('Incomplete asynchronous Lean trace')
    previous, results = ('0', '0'), []
    for i, row in enumerate(raw):
        c, r = ALPHABET[encoded[i]['clock']], ALPHABET[encoded[i]['reset']]
        cr, rr = (previous[0], c) in POSEDGE, (previous[1], r) in POSEDGE
        expected = dict(sample=i, clock=encoded[i]['clock'], reset=encoded[i]['reset'], clock_rising=cr, reset_rising=rr, fired=cr or rr)
        if any(row[k] != v for k, v in expected.items()):
            raise ValueError('Actual Lean event detection mismatch at sample ' + str(i))
        for phase in ('pre', 'post'):
            expected[phase] = dict(outputs=decode(mapping, row[phase]['outputs']),
                                   states=decode({'ports': state_ports(mapping)}, row[phase]['states']))
        results.append(expected); previous = (c, r)
    write(evidence / 'fourstate-result.json', results)
    return results
