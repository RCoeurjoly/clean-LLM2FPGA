"""Build a pure Lean cached evaluator with compact physical-input requests.

Unchanged node operands reuse the previous node value AND definedness. Optional
full audits compare the complete Evaluation, including every next-state field,
against Functional.evaluate. The baseline compiler and evaluator stay intact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from build_lean_table_runner import RUNNER
from common import ROOT, lean_executable, lean_runtime_files, run_logged
from yosys_hierarchy import digest


COMPACT = '''
def compactPositions (circuit : Circuit) (header : Lean.Json) : Except String (Array Nat) := do
  let names ← header.getObjValAs? (Array String) "input_order"
  let mut indices : Std.HashMap String Nat := {}
  for i in [:circuit.inputs.size] do
    indices := indices.insert (← arrayAt circuit.inputs i).name i
  let mut seen : Std.HashSet String := {}
  let mut result := #[]
  for name in names do
    require (!seen.contains name) ("Duplicate compact input " ++ name)
    seen := seen.insert name
    let some i := indices[name]? | throw ("Unknown compact input " ++ name)
    match (← arrayAt circuit.inputs i).sort with
    | .bits _ => result := result.push i
    | _ => throw "Compact input vectors require bitvector ports"
  return result

def compactFrame (circuit : Circuit) (defaults : Array Value) (positions : Array Nat)
    (json : Lean.Json) : Except String (Array Value) := do
  let data ← Lean.fromJson? (α := Array Int) json
  require (data.size == positions.size) "Compact input vector length mismatch"
  let mut values := defaults
  for k in [:positions.size] do
    let i ← arrayAt positions k
    let field ← arrayAt circuit.inputs i
    match field.sort with
    | .bits width => values := values.set! i (.bits width (BitVec.ofInt width (← arrayAt data k)))
    | _ => throw "Compact input vectors require bitvector ports"
  return values
'''


def runner_source():
    text = RUNNER.replace('import Chisel2Lean.Functional', 'import Chisel2Lean.Incremental', 1)
    text = text.replace('def main (args : List String)', COMPACT + '\ndef main (args : List String)', 1)
    text = text.replace('  let eval (inputs state : Array Value)', '''  let defaults ← match header.getObjVal? "input_defaults" with
    | .ok json => do
      let frame ← unwrap (decodeFrame circuit.inputs json)
      let positions ← unwrap (compactPositions circuit header)
      pure (some (frame, positions))
    | .error _ => pure none
  let auditEvery := (header.getObjValAs? Nat "audit_every").toOption.getD 0
  let cached ← IO.mkRef (none : Option (IncrementalCache circuit))
  let evaluations ← IO.mkRef (0 : Nat)
  let spent ← IO.mkRef (0 : Nat)
  let eval (inputs state : Array Value)''', 1)
    text = text.replace('    let result ← unwrap (evaluate circuit inputs state)', '''    let start ← IO.monoMsNow
    let (result, nextCache) ← unwrap (evaluateIncremental circuit inputs state (← cached.get))
    let number ← evaluations.get
    if auditEvery > 0 && number % auditEvery == 0 then
      let original ← unwrap (evaluate circuit inputs state)
      if result ≠ original then throw (IO.userError s!"Incremental/full evaluation mismatch at evaluation {number}")
    cached.set (some nextCache)
    evaluations.set (number + 1)
    let finish ← IO.monoMsNow
    spent.modify (· + finish - start)''', 1)
    # The clock-mode handling and state-sharing code remain exactly the baseline.
    old = '    let inputs ← unwrap (decodeFrame circuit.inputs (← unwrap (row.getObjVal? "inputs")))'
    new = '''    let raw ← unwrap (row.getObjVal? "inputs")
    let inputs ← match defaults with
      | none => unwrap (decodeFrame circuit.inputs raw)
      | some (frame, positions) => unwrap (compactFrame circuit frame positions raw)'''
    if old not in text:
        raise ValueError('Baseline request decoder changed; review compact input adaptation')
    text = text.replace(old, new, 1)
    text += '\n'
    text = text.replace('    cycle := cycle + 1', '''    cycle := cycle + 1
    if cycle % 32 == 0 then IO.eprintln s!"C2L_PROGRESS samples={cycle} evaluations={← evaluations.get} evaluator_ms={← spent.get}"''', 1)
    return text


def build(folder):
    folder = folder.resolve()
    if folder.exists():
        raise ValueError('Keep existing runners; choose a fresh directory')
    folder.mkdir(parents=True)
    sources = [ROOT/'lean/Chisel2Lean/Functional.lean', ROOT/'lean/Chisel2Lean/Incremental.lean']
    files = []
    for source in sources:
        target = folder/'Chisel2Lean'/source.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
        files.append(target)
    main = folder/'RunIncremental.lean'
    main.write_text(runner_source(), encoding='utf-8')
    files.append(main)
    tools = Path(lean_executable()).parent
    env = os.environ.copy()
    env['LEAN_PATH'] = str(folder)
    env['PATH'] = str(tools) + os.pathsep + env.get('PATH','')
    for source in files:
        if run_logged([lean_executable(), '-o', str(source.with_suffix('.olean')), '-c', str(source.with_suffix('.c')), str(source)],
                      folder/('compile-'+source.stem), cwd=folder, env=env, echo=False):
            raise RuntimeError('Lean compilation failed: ' + str(folder/('compile-'+source.stem)))
        if 'sorryAx' in (folder/('compile-'+source.stem)/'stdout.log').read_text(encoding='utf-8'):
            raise RuntimeError('Unfinished Lean proof in runner')
    executable = folder/'run-table'
    if run_logged([str(tools/'leanc'), '-O2', '-o', str(executable), *[str(p.with_suffix('.c')) for p in files]],
                  folder/'native-link', cwd=folder, env=env, echo=False):
        raise RuntimeError('Native compilation failed')
    report = {'status':'COMPILED', 'runtime_sha256':digest(files[0]),
              'incremental_source_sha256':digest(files[1]), 'runner_source_sha256':digest(main),
              'executable':str(executable),'executable_sha256':digest(executable),
              'tools_sha256':{n:digest(p) for n,p in lean_runtime_files().items()},
              'scope':'Pure Lean incremental execution; regression and full-evaluation audits required before acceptance.'}
    (folder/'manifest.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.out),indent=2))
