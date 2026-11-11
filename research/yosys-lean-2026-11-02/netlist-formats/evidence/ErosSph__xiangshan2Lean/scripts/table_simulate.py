"""Replay an explicit request through a compiled Lean table model."""
from __future__ import annotations

import json
import os

from common import lean_executable, run_logged
from pathlib import Path
from yosys_hierarchy import digest


def load_interface(path):
    if not path.name.endswith('.table.json') or path.stat().st_size < 64 * 1024 * 1024:
        return json.loads(path.read_text(encoding='utf-8'))
    from bootstrap_json_stream import reader
    result = {'inputs': [], 'states': [], 'arbitrary_inputs': []}
    names = {'inputs.item.name': 'inputs', 'states.item.name': 'states',
             'arbitrary_inputs.item.name': 'arbitrary_inputs'}
    with path.open('rb') as stream:
        for prefix, event, value in reader().parse(stream):
            if prefix in {'namespace', 'clock', 'lean_backend'} and event in {'string', 'null'}:
                result[prefix] = value
            elif prefix in names and event == 'string':
                result[names[prefix]].append({'name': value})
    if result.get('lean_backend') != 'table' or 'namespace' not in result or 'clock' not in result:
        raise ValueError('Incomplete Lean table interface')
    return result


def simulate_table(path, ir, trace, initial, evidence, native_runner=None):
    if not trace:
        raise ValueError('Trace is empty')
    if set(initial) != {field['name'] for field in ir['states']}:
        raise ValueError('Supply every state field explicitly')
    clock = ir['clock']
    names = {field['name'] for field in ir['inputs']} - {clock}
    if any(set(row['inputs']) != names for row in trace):
        raise ValueError('Inputs must match the model, except its clock supplied by the cycle driver')
    evidence.mkdir(parents=True, exist_ok=True)
    runner = json.loads((Path(native_runner) / 'manifest.json').read_text(encoding='utf-8')) if native_runner else None
    streamed = runner and runner.get('request_format') == 'jsonl-v1'
    request = evidence / ('request.jsonl' if streamed else 'request.json')
    with request.open('w', encoding='utf-8') as stream:
        if streamed:
            json.dump({'initial': initial}, stream, separators=(',', ':'))
            stream.write('\n')
        else:
            stream.write('{"initial":')
            json.dump(initial, stream, separators=(',', ':'))
            stream.write(',"trace":[')
        for index, row in enumerate(trace):
            values = dict(row['inputs'])
            if clock:
                values[clock] = 0
            if not streamed and index:
                stream.write(',')
            json.dump({'inputs': values}, stream, separators=(',', ':'))
            if streamed:
                stream.write('\n')
        if not streamed:
            stream.write(']}')
    source = evidence / 'Run.lean'
    source.write_text(f'''import {ir['namespace']}
def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply request path")
  let request ← Chisel2Lean.Functional.unwrap (Lean.Json.parse (← IO.FS.readFile path))
  let circuit ← Chisel2Lean.Functional.unwrap {ir['namespace']}.circuit
  Chisel2Lean.Functional.runRequest circuit request
''', encoding='utf-8')
    root = path.resolve()
    for _ in ir['namespace'].split('.'):
        root = root.parent
    env = os.environ.copy()
    env['LEAN_PATH'] = str(root)
    command = [lean_executable(), '--run', str(source.resolve()), str(request.resolve())]
    binary = None
    if native_runner is not None:
        manifest_path = root / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if runner['status'] != 'PASS' or manifest.get('lean_backend') != 'table' or not manifest['lean_checked']:
            raise ValueError('Native replay requires a compiled, validated Lean table model')
        if runner['runtime_sha256'] != manifest['table_runtime_sha256']:
            raise ValueError('Native evaluator differs from the compiled Lean model runtime')
        executable = Path(runner['executable'])
        if digest(executable) != runner['executable_sha256']:
            raise ValueError('Native Lean executable changed')
        tools = Path(lean_executable()).parent
        for name, expected in runner['tools_sha256'].items():
            if digest(tools / name) != expected:
                raise ValueError('Native Lean dependency changed: ' + name)
        binary = (root / manifest['table']['binary']).resolve()
        if not binary.is_relative_to(root) or digest(binary) != manifest['artifact_sha256'][binary.relative_to(root).as_posix()]:
            raise ValueError('Lean circuit binary differs from the compiled model')
        env['PATH'] = str(tools) + os.pathsep + env.get('PATH', '')
        command = [str(executable), str(binary), str(request.resolve())]
        engine = {'engine': 'native compiled Lean', 'runtime_sha256': runner['runtime_sha256'],
                  'request_format': runner.get('request_format', 'json'),
                  'executable_sha256': runner['executable_sha256'], 'binary_sha256': digest(binary),
                  'model_manifest_sha256': digest(manifest_path)}
        (evidence / 'engine.json').write_text(json.dumps(engine, indent=2), encoding='utf-8')
    code = run_logged(command, evidence / 'execution', cwd=root, env=env, echo=False)
    if binary is not None and digest(binary) != engine['binary_sha256']:
        raise ValueError('Circuit binary changed during native Lean execution')
    rows = [] if code else [json.loads(line) for line in (evidence / 'execution/stdout.log').read_text(encoding='utf-8').splitlines() if line.strip()]
    if not code and len(rows) != len(trace):
        raise RuntimeError('Incomplete Lean table trace')
    (evidence / 'result.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return code, rows
