import Chisel2Lean.Functional

open Chisel2Lean.Functional

-- One concrete completion of unknown initial bits, used only for this trace.
def traceInitial (field : Field) : Except String Value := do
  match field.sort with
  | .bits width =>
    let known ← arrayAt field.known 0
    return .bits width (BitVec.ofNat width known.2)
  | .memory addressWidth width =>
    return .memory addressWidth width (field.known.map fun known => BitVec.ofNat width known.2)
  | .boolean => throw "Unexpected Boolean state"

def traceInputs (fields : Array Field) (clock reset : Nat) : Except String (Array Value) :=
  fields.mapM fun field => do
    match field.sort with
    | .bits width =>
      let value := if field.name == "clk" then clock else if field.name == "reset" then reset else 0
      return .bits width (BitVec.ofNat width value)
    | _ => throw "Unexpected non-bitvector input"

def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply circuit binary")
  let checked ← unwrap (decode (← IO.FS.readBinFile path))
  let circuit := checked.data
  IO.println ("C2L_TABLE_CHECK " ++ (Lean.Json.mkObj [
    ("nodes", Lean.toJson circuit.nodes.size),
    ("inputs", Lean.toJson circuit.inputs.size),
    ("states", Lean.toJson circuit.states.size),
    ("outputs", Lean.toJson circuit.outputs.size)]).compress)
  let mut state ← unwrap (circuit.states.mapM traceInitial)
  if !(← unwrap (initialValid circuit.states state)) then
    throw (IO.userError "Invalid concrete initial completion")
  let samples := #[(0, 1), (1, 1), (0, 1), (1, 1), (0, 0), (1, 0), (0, 0), (1, 0)]
  for index in [:samples.size] do
    let (clock, reset) := samples[index]!
    let inputs ← unwrap (traceInputs circuit.inputs clock reset)
    let result ← unwrap (evaluate circuit inputs state)
    if !result.outputDefined || !result.transitionDefined || !result.checksValid then
      throw (IO.userError s!"Undefined operation or failed check at sample {index}")
    IO.println ((Lean.Json.mkObj [
      ("sample", Lean.toJson index),
      ("outputs", ← unwrap (encodeOutputs circuit result.outputs))]).compress)
    state := result.nextState
