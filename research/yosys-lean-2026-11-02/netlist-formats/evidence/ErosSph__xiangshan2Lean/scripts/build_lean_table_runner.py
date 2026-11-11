"""Compile the same pure Lean table evaluator to a native replay executable."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from common import ROOT, lean_executable, lean_runtime_files, run_logged
from yosys_hierarchy import digest

RUNNER = '''import Chisel2Lean.Functional
open Chisel2Lean.Functional
deriving instance DecidableEq for Value

abbrev SmallValuePool := Std.HashMap (Nat × Nat) Value

def poolValue (pool : SmallValuePool) (key : Nat × Nat) (value : Value) : Value :=
  match pool[key]? with
  | none => value
  | some cached => if cached = value then cached else value

@[simp] theorem poolValue_eq (pool : SmallValuePool) (key : Nat × Nat) (value : Value) :
    poolValue pool key value = value := by
  unfold poolValue
  split
  · rfl
  · split <;> simp_all

def shareSmallValue (pool : SmallValuePool) (value : Value) : Value :=
  match value with
  | .bits width word =>
    if word.toNat ≤ 1 then poolValue pool (width, word.toNat) value else value
  | _ => value

@[simp] theorem shareSmallValue_eq (pool : SmallValuePool) (value : Value) :
    shareSmallValue pool value = value := by
  cases value <;> simp [shareSmallValue]

def shareState (pool : SmallValuePool) (state : Array Value) : Array Value :=
  state.map (shareSmallValue pool)

theorem shareState_eq (pool : SmallValuePool) (state : Array Value) :
    shareState pool state = state := by
  exact Array.map_id'' (shareSmallValue_eq pool) state

def extendPool (pool : SmallValuePool) (state : Array Value) : SmallValuePool := Id.run do
  let mut result := pool
  for value in state do
    match value with
    | .bits width word =>
      if word.toNat ≤ 1 && !result.contains (width, word.toNat) then
        result := result.insert (width, word.toNat) value
    | _ => pure ()
  return result

#print axioms shareState_eq

def main (args : List String) : IO Unit := do
  let [circuitPath, requestPath] := args | throw (IO.userError "Supply circuit and request paths")
  let checked ← unwrap (decode (← IO.FS.readBinFile circuitPath))
  let circuit := checked.data
  let input ← IO.FS.Handle.mk requestPath .read
  let header ← unwrap (Lean.Json.parse (← input.getLine))
  let mut state ← unwrap (decodeFrame circuit.states (← unwrap (header.getObjVal? "initial")))
  if !(← unwrap (initialValid circuit.states state)) then throw (IO.userError "Initial state violates known bits")
  let mut pool := extendPool {} state
  state := shareState pool state
  let eval (inputs state : Array Value) : IO Evaluation := do
    let result ← unwrap (evaluate circuit inputs state)
    if result.maxLeftShift > 1000000 then
      IO.eprintln s!"C2L_LARGE_SHIFT amount={result.maxLeftShift} smt_line={result.maxLeftShiftLine}"
    if !result.outputDefined || !result.transitionDefined then throw (IO.userError "Undefined operation")
    if !result.checksValid then throw (IO.userError "Assertion or assumption failed")
    return result
  let mut cycle : Nat := 0
  repeat
    let line ← input.getLine
    if line.isEmpty then break
    let row ← unwrap (Lean.Json.parse line)
    let inputs ← unwrap (decodeFrame circuit.inputs (← unwrap (row.getObjVal? "inputs")))
    let (pre, post, nextState) ← match circuit.clock with
      | none => do
        let result ← eval inputs state
        pure (result.outputs, result.outputs, result.nextState)
      | some index => do
        if h : index < inputs.size then
          let low ← eval (inputs.set index (.bits 1 0)) state
          let high ← eval (inputs.set index (.bits 1 1)) low.nextState
          pure (low.outputs, high.outputs, high.nextState)
        else throw (IO.userError "Clock index outside input frame")
    let mut fields := [("cycle", Lean.toJson cycle), ("pre", ← unwrap (encodeOutputs circuit pre)),
                       ("post", ← unwrap (encodeOutputs circuit post))]
    if !circuit.effects.isEmpty then
      fields := fields ++ [("effects_pre", ← unwrap (encodeEffects circuit pre)),
                           ("effects_post", ← unwrap (encodeEffects circuit post))]
    IO.println (Lean.Json.mkObj fields).compress
    pool := extendPool pool nextState
    state := shareState pool nextState
    cycle := cycle + 1
'''


def build_runner(runtime_source, folder):
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    runtime = folder / 'Chisel2Lean/Functional.lean'
    runtime.parent.mkdir(exist_ok=True)
    shutil.copy2(runtime_source, runtime)
    source = folder / 'RunTable.lean'
    source.write_text(RUNNER, encoding='utf-8')
    tools = Path(lean_executable()).parent
    cc = tools / ('leanc.exe' if os.name == 'nt' else 'leanc')
    env = os.environ.copy()
    env['LEAN_PATH'] = str(folder)
    env['PATH'] = str(tools) + os.pathsep + env.get('PATH', '')
    temporary = folder / 'tmp'
    temporary.mkdir(exist_ok=True)
    for name in ('TMP', 'TEMP', 'TMPDIR'):
        env[name] = str(temporary)
    for name, path in [('runtime-c', runtime), ('runner-c', source)]:
        if run_logged([lean_executable(), '-o', str(path.with_suffix('.olean')), '-c', str(path.with_suffix('.c')), str(path)],
                      folder / name, cwd=folder, env=env, echo=False):
            raise RuntimeError('Native Lean C generation failed: ' + str(folder / name))
        if 'sorryAx' in (folder / name / 'stdout.log').read_text(encoding='utf-8'):
            raise RuntimeError('Native Lean proof contains an unfinished placeholder')
    executable = folder / ('run-table.exe' if os.name == 'nt' else 'run-table')
    if run_logged([str(cc), '-O2', '-o', str(executable), str(runtime.with_suffix('.c')), str(source.with_suffix('.c'))],
                  folder / 'native-link', cwd=folder, env=env, echo=False):
        raise RuntimeError('Native Lean compilation failed: ' + str(folder / 'native-link'))
    report = {'status': 'PASS', 'request_format': 'jsonl-v1', 'runtime_sha256': digest(runtime), 'runner_source_sha256': digest(source),
              'executable': str(executable), 'executable_sha256': digest(executable),
              'tools_sha256': {name: digest(path) for name, path in lean_runtime_files().items()},
              'scope': 'Native compilation of the same Lean evaluator and validated binary decoder; no circuit-specific rewrite.'}
    (folder / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / 'lean/Chisel2Lean/Functional.lean')
    parser.add_argument('--out', type=Path, default=ROOT / 'experiments/lean-table-native-7.3')
    args = parser.parse_args()
    print(json.dumps(build_runner(args.runtime.resolve(), args.out), indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
