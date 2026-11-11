"""Compile a pure Lean runner that continues its own complete next state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from build_incremental_runner import COMPACT
from build_lean_table_runner import RUNNER
from common import ROOT, lean_executable, lean_runtime_files, run_logged
from yosys_hierarchy import digest
from compiled_runtime import source as compiled_runtime_source

STATE_OBSERVATIONS = '''
def encodeFastOutputs (circuit : Circuit) (values : Array Value) : Except String Lean.Json := do
  let mut fields := []
  for index in [:circuit.outputs.size] do
    let output ← arrayAt circuit.outputs index
    if !output.observation then
      fields := (output.name, (← arrayAt values index).toJson) :: fields
  return Lean.Json.mkObj fields.reverse

def stateObservationPositions (circuit : Circuit) (header : Lean.Json) : Except String (Array Nat) := do
  let names ← match header.getObjVal? "observe_state" with
    | .error _ => pure (#[] : Array String)
    | .ok value => Lean.fromJson? value
  if !names.isEmpty then require circuit.clock.isNone "State observations require explicit global sampling"
  let mut seen : Std.HashSet String := {}
  let mut result := #[]
  for name in names do
    require (!seen.contains name) ("Duplicate state observation " ++ name)
    seen := seen.insert name
    let mut found := none
    for i in [:circuit.states.size] do
      if (← arrayAt circuit.states i).name == name then
        found := some i
        break
    let some i := found | throw ("Unknown state observation " ++ name)
    result := result.push i
  return result

def encodeObservedState (circuit : Circuit) (positions : Array Nat) (state : Array Value) : Except String Lean.Json := do
  let mut fields := []
  for i in positions do
    fields := fields ++ [((← arrayAt circuit.states i).name, (← arrayAt state i).toJson)]
  return Lean.Json.mkObj fields
'''


def runner_source():
    text = RUNNER.replace('import Chisel2Lean.Functional', 'import Chisel2Lean.Progressive', 1)
    text = text.replace('def main (args : List String)', COMPACT + STATE_OBSERVATIONS + '\ndef main (args : List String)', 1)
    start = text.index('  let eval (inputs state : Array Value)')
    end = text.index('  let mut cycle : Nat := 0', start)
    text = text[:start] + '''  let initialState := state
  let defaults ← match header.getObjVal? "input_defaults" with
    | .ok json => do
      let frame ← unwrap (decodeFrame circuit.inputs json)
      let positions ← unwrap (compactPositions circuit header)
      pure (some (frame, positions))
    | .error _ => pure none
  let statePositions ← unwrap (stateObservationPositions circuit header)
  let auditEvery := (header.getObjValAs? Nat "audit_every").toOption.getD 0
  let cached ← IO.mkRef (none : Option (ProgressCache circuit))
  let evaluations ← IO.mkRef (0 : Nat)
  let spent ← IO.mkRef (0 : Nat)
  let eval (inputs : Array Value) : IO Evaluation := do
    let start ← IO.monoMsNow
    let old ← cached.get
    cached.set none
    let number ← evaluations.get
    let original ← if auditEvery > 0 && number % auditEvery == 0 then do
      let auditState := match old with
        | none => initialState
        | some previous => previous.result.nextState
      pure (some (← unwrap (evaluate circuit inputs auditState)))
    else pure none
    let (result, nextCache) ← unwrap (match old with
      | none => beginProgressive circuit inputs initialState
      | some previous => do
        match defaults with
        | none => advanceProgressive circuit inputs previous
        | some (_, originalPositions) =>
          let positions := match circuit.clock with
            | none => originalPositions
            | some clock => if originalPositions.contains clock then originalPositions else originalPositions.push clock
          let updates ← positions.mapM fun i => do return (i, ← arrayAt inputs i)
          advanceProgressiveUpdates circuit updates previous)
    if let some full := original then
      if result ≠ full then throw (IO.userError s!"Progressive/full evaluation mismatch at evaluation {number}")
    cached.set (some nextCache)
    evaluations.set (number + 1)
    let finish ← IO.monoMsNow
    spent.modify (· + finish - start)
    if result.maxLeftShift > 1000000 then
      IO.eprintln s!"C2L_LARGE_SHIFT amount={result.maxLeftShift} smt_line={result.maxLeftShiftLine}"
    if !result.outputDefined || !result.transitionDefined then throw (IO.userError "Undefined operation")
    if !result.checksValid then throw (IO.userError "Assertion or assumption failed")
    return result
''' + text[end:]
    text = text.replace('    let inputs ← unwrap (decodeFrame circuit.inputs (← unwrap (row.getObjVal? "inputs")))', '''    let raw ← unwrap (row.getObjVal? "inputs")
    let inputs ← match defaults with
      | none => unwrap (decodeFrame circuit.inputs raw)
      | some (frame, positions) => unwrap (compactFrame circuit frame positions raw)''', 1)
    text = text.replace('eval inputs state', 'eval inputs')
    text = text.replace('eval (inputs.set index (.bits 1 0)) state', 'eval (inputs.set index (.bits 1 0))')
    text = text.replace('eval (inputs.set index (.bits 1 1)) low.nextState', 'eval (inputs.set index (.bits 1 1))')
    text = text.replace('    pool := extendPool pool nextState\n    state := shareState pool nextState\n', '')
    text = text.replace('    let mut fields := [("cycle", Lean.toJson cycle), ("pre", ← unwrap (encodeOutputs circuit pre)),\n                       ("post", ← unwrap (encodeOutputs circuit post))]', '''    let encodedPre ← unwrap (encodeFastOutputs circuit pre)
    let encodedPost ← if circuit.clock.isNone then pure encodedPre else unwrap (encodeFastOutputs circuit post)
    let mut fields := [("cycle", Lean.toJson cycle), ("pre", encodedPre), ("post", encodedPost)]''', 1)
    text = text.replace('      fields := fields ++ [("effects_pre", ← unwrap (encodeEffects circuit pre)),\n                           ("effects_post", ← unwrap (encodeEffects circuit post))]', '''      let encodedEffects ← unwrap (encodeEffects circuit pre)
      let encodedEffectsPost ← if circuit.clock.isNone then pure encodedEffects else unwrap (encodeEffects circuit post)
      fields := fields ++ [("effects_pre", encodedEffects), ("effects_post", encodedEffectsPost)]''', 1)
    text = text.replace('    IO.println (Lean.Json.mkObj fields).compress', '''    if !statePositions.isEmpty then
      let some current ← cached.get | throw (IO.userError "Missing observation evaluation")
      fields := fields ++ [("state_before", ← unwrap (encodeObservedState circuit statePositions current.base.state)),
                           ("state_after", ← unwrap (encodeObservedState circuit statePositions current.result.nextState))]
    IO.println (Lean.Json.mkObj fields).compress''', 1)
    text = text.replace('    cycle := cycle + 1', '''    cycle := cycle + 1
    if cycle % 32 == 0 then IO.eprintln s!"C2L_PROGRESS samples={cycle} evaluations={← evaluations.get} evaluator_ms={← spent.get}"''', 1)
    return text


def build(folder):
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    files = []
    for name in ('Functional.lean', 'Incremental.lean', 'Compiled.lean', 'Progressive.lean'):
        target = folder / 'Chisel2Lean' / name
        target.parent.mkdir(exist_ok=True)
        if name == 'Compiled.lean':
            target.write_text(compiled_runtime_source(), encoding='utf-8')
        else:
            shutil.copy2(ROOT / 'lean/Chisel2Lean' / name, target)
        files.append(target)
    main = folder / 'RunProgressive.lean'
    main.write_text(runner_source(), encoding='utf-8')
    files.append(main)
    tools = Path(lean_executable()).parent
    env = os.environ.copy()
    env['LEAN_PATH'] = str(folder)
    env['PATH'] = str(tools) + os.pathsep + env.get('PATH', '')
    for source in files:
        if run_logged([lean_executable(), '-o', str(source.with_suffix('.olean')), '-c', str(source.with_suffix('.c')), str(source)],
                      folder / ('compile-'+source.stem), cwd=folder, env=env, echo=False):
            raise RuntimeError('Lean compilation failed: ' + str(folder / ('compile-'+source.stem)))
        if 'sorryAx' in (folder / ('compile-'+source.stem) / 'stdout.log').read_text(encoding='utf-8'):
            raise RuntimeError('Unfinished Lean proof in runner')
    executable = folder / 'run-table'
    if run_logged([str(tools / 'leanc'), '-O2', '-o', str(executable), *[str(p.with_suffix('.c')) for p in files]],
                  folder / 'native-link', cwd=folder, env=env, echo=False):
        raise RuntimeError('Native compilation failed')
    report = {'status': 'COMPILED', 'runtime_sha256': digest(files[0]),
              'incremental_source_sha256': digest(files[1]), 'compiled_source_sha256': digest(files[2]),
              'progressive_source_sha256': digest(files[3]),
              'runner_source_sha256': digest(main), 'executable': str(executable),
              'executable_sha256': digest(executable),
              'tools_sha256': {name: digest(path) for name, path in lean_runtime_files().items()},
              'scope': 'Pure Lean continuation of every complete next state. No RTL state import or time skipping. Full evaluation audits and independent regressions required.'}
    (folder / 'manifest.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.out), indent=2))
