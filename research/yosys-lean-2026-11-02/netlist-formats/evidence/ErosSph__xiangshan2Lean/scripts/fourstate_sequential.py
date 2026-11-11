"""Explicit four-state register state and sampling wrapper; transitions execute in Lean."""
from __future__ import annotations

import json
import os
from pathlib import Path

from common import lean_executable, run_logged
from fourstate2lean import fresh_directory, write
from lean_records import access_paths, record_expression


def state_ports(mapping):
    return [dict(p, direction='output', tag=p['current_tag'], value=p['current_value']) for p in mapping['states']]


def validate(mapping, trace, initial):
    if 'events' in mapping:
        from fourstate_events import validate as validate_events
        return validate_events(mapping, trace, initial)
    from fourstate_simulate import encode_trace
    states = mapping['states']
    if not isinstance(initial, dict) or set(initial) != {p['name'] for p in states}:
        raise ValueError('Supply exactly every original register in --initial-file')
    init_mapping = {'ports': [dict(p, direction='input', tag=p['current_tag'], value=p['current_value']) for p in states]}
    encoded_initial = encode_trace(init_mapping, [{'inputs': initial}])[0]['inputs']
    clock = mapping['clock']
    names = {p['name'] for p in mapping['ports'] if p['direction'] == 'input'}
    if not isinstance(trace, list) or not trace:
        raise ValueError('Nonempty sampling trace required')
    previous = None
    data_rows, clocks = [], []
    for row in trace:
        if not isinstance(row, dict) or set(row) != {'inputs'} or not isinstance(row['inputs'], dict) or set(row['inputs']) != names:
            raise ValueError('Trace must supply exactly all original inputs including clock')
        values = row['inputs']
        if values[clock] not in ('0', '1'):
            raise ValueError('Clock must be the binary string 0 or 1')
        if previous is None and values[clock] != '0':
            raise ValueError('First sample clock must be 0')
        data = {k: v for k, v in values.items() if k != clock}
        if previous is not None and previous[clock] == '0' and values[clock] == '1':
            if data != {k: v for k, v in previous.items() if k != clock}:
                raise ValueError('Data and controls must be stable before a rising edge')
        data_rows.append({'inputs': data}); clocks.append(int(values[clock])); previous = values
    data_mapping = {'ports': [p for p in mapping['ports'] if p.get('role') != 'clock']}
    encoded = encode_trace(data_mapping, data_rows)
    return [{'clock': c, **row} for c, row in zip(clocks, encoded)], encoded_initial


def runner_source(ir, mapping):
    group = ir.get('lean_record_group_size', 128)
    paths = access_paths((p['field'] for p in ir['outputs']), group)
    outputs = {p['name']: 'o.' + paths[p['field']] for p in ir['outputs']}
    current = {p[k] for p in mapping['states'] for k in ('current_tag', 'current_value')}
    bindings, lines = [], ['def decodeInputs (j regs : Lean.Json) : Except String Inputs := do']
    for index, p in enumerate(ir['inputs']):
        source = 'regs' if p['name'] in current else 'j'
        lines.append(f'  let v{index} ← {source}.getObjValAs? Nat {json.dumps(p["name"])}')
        bindings.append((p['field'], f'BitVec.ofNat {p["sort"]["width"]} v{index}'))
    lines.append('  return ' + record_expression(bindings, group))
    def json_out(pairs):
        return 'Lean.Json.mkObj [' + ', '.join(f'({json.dumps(name)}, Lean.toJson {outputs[value]}.toNat)' for name, value in pairs) + ']'
    port_pairs = [(p[k], p[k]) for p in mapping['ports'] if p['direction'] == 'output' for k in ('tag', 'value')]
    state_pairs = [(p['current_' + k], p['next_' + k]) for p in mapping['states'] for k in ('tag', 'value')]
    return f'''import {ir['namespace']}
set_option maxRecDepth 65536
set_option maxHeartbeats 0
set_option linter.unusedVariables false
open {ir['namespace']}
{chr(10).join(lines)}
def encodeOutputs (o : Outputs) : Lean.Json := {json_out(port_pairs)}
def encodeNext (o : Outputs) : Lean.Json := {json_out(state_pairs)}
def unwrap (r : Except String α) : IO α :=
  match r with
  | .ok x => pure x
  | .error e => throw (IO.userError e)
def checked (inputs : Inputs) : IO Outputs := do
  let r := evaluate inputs ⟨⟩
  if !r.outputDefined || !r.transitionDefined then
    throw (IO.userError "Undefined transition operation")
  return r.outputs
def snapshot (outputs regs : Lean.Json) : Lean.Json :=
  Lean.Json.mkObj [("outputs", outputs), ("states", regs)]
def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply request path")
  let request ← unwrap (Lean.Json.parse (← IO.FS.readFile path))
  let rows ← unwrap (request.getObjValAs? (Array Lean.Json) "trace")
  let mut regs ← unwrap (request.getObjVal? "initial")
  let mut previous : Nat := 0
  let mut sample : Nat := 0
  for row in rows do
    let clock ← unwrap (row.getObjValAs? Nat "clock")
    if clock > 1 || (sample == 0 && clock != 0) then
      throw (IO.userError "Invalid sampling clock")
    let j ← unwrap (row.getObjVal? "inputs")
    let inputs ← unwrap (decodeInputs j regs)
    let before ← checked inputs
    let pre := snapshot (encodeOutputs before) regs
    let rising := previous == 0 && clock == 1
    if rising then
      regs := encodeNext before
    let after ← checked (← unwrap (decodeInputs j regs))
    let post := snapshot (encodeOutputs after) regs
    IO.println ((Lean.Json.mkObj [("sample", Lean.toJson sample),
      ("clock", Lean.toJson clock), ("rising", Lean.toJson rising),
      ("pre", pre), ("post", post)]).compress)
    previous := clock
    sample := sample + 1
'''


def simulate(mapping, ir_path, trace, initial, evidence):
    if mapping.get('events', {}).get('format') == 'fourstate-bounded-events-v1':
        from fourstate_bounded_events import simulate as simulate_bounded
        return simulate_bounded(mapping,ir_path,trace,initial,evidence)
    if 'events' in mapping:
        from fourstate_events import simulate as simulate_events
        return simulate_events(mapping, ir_path, trace, initial, evidence)
    from fourstate_simulate import decode
    encoded, encoded_initial = validate(mapping, trace, initial)
    evidence = fresh_directory(evidence)
    write(evidence / 'fourstate-request.json', {'trace': trace, 'initial': initial})
    write(evidence / 'request.json', {'trace': encoded, 'initial': encoded_initial})
    ir = json.loads(Path(ir_path).read_text(encoding='utf-8'))
    source = evidence / 'RunSequential.lean'
    source.write_text(runner_source(ir, mapping), encoding='utf-8')
    root = Path(ir_path)
    for _ in ir['namespace'].split('.'):
        root = root.parent
    env = os.environ.copy(); env['LEAN_PATH'] = str(root)
    if run_logged([lean_executable(), '--run', str(source), str(evidence / 'request.json')],
                  evidence / 'lean-execution', cwd=root, env=env, echo=False):
        raise RuntimeError('Sequential Lean execution failed; see ' + str(evidence / 'lean-execution'))
    raw = [json.loads(line) for line in (evidence / 'lean-execution/stdout.log').read_text().splitlines() if line.strip()]
    write(evidence / 'encoded-result.json', raw)
    if len(raw) != len(trace):
        raise ValueError('Incomplete sequential Lean trace')
    results = []
    previous = 0
    for i, row in enumerate(raw):
        clock = encoded[i]['clock']
        if row['sample'] != i or row['clock'] != clock or row['rising'] != (previous == 0 and clock == 1):
            raise ValueError('Sequential Lean sampling metadata mismatch')
        result = {k: row[k] for k in ('sample', 'clock', 'rising')}
        for phase in ('pre', 'post'):
            result[phase] = {'outputs': decode(mapping, row[phase]['outputs']),
                             'states': decode({'ports': state_ports(mapping)}, row[phase]['states'])}
        results.append(result); previous = clock
    write(evidence / 'fourstate-result.json', results)
    return results
