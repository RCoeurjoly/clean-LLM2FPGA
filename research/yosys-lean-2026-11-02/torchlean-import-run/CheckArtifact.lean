import NN.Runtime.IR

def main (args : List String) : IO Unit := do
  let path :: rest := args | throw (IO.userError "Supply graph JSON and optional numerical request")
  let artifact ← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile path))
  let program ← IO.ofExcept (Import.IR.parseProgram artifact)
  let mut fields := [
    ("nodes", Lean.toJson program.graph.nodes.size),
    ("inputs", Lean.toJson program.inputs.size),
    ("outputs", Lean.toJson program.outputs.size)]
  match rest with
  | [] => pure ()
  | [requestPath] =>
    let request ← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile requestPath))
    let inputWords ← IO.ofExcept (request.getObjValAs? (Array (Array Nat)) "input_words")
    unless inputWords.size == program.inputs.size do
      throw (IO.userError "Numerical control input count mismatch")
    let mut inputs : Array (NN.IR.Interpreter.Value NN.IR.Interpreter.reference) := #[]
    for index in [:program.inputs.size] do
      let id := program.inputs[index]!
      let node ← IO.ofExcept (program.graph.getNode id)
      let dtype ← IO.ofExcept (program.dtype id)
      let value ← IO.ofExcept (NN.IR.Reference.load dtype node.outShape inputWords[index]!)
      inputs := inputs.push value
    let payload ← IO.ofExcept (Import.IR.parsePayload NN.IR.Interpreter.reference NN.IR.Reference.load artifact)
    let (outputs, _) ← IO.ofExcept (NN.IR.Interpreter.eval NN.IR.Interpreter.reference program payload inputs)
    let words ← IO.ofExcept (outputs.mapM NN.IR.Reference.words)
    let expected ← IO.ofExcept (request.getObjValAs? (Array (Array Nat)) "expected_output_words")
    unless words == expected do
      throw (IO.userError s!"Numerical control mismatch: expected {expected}, got {words}")
    fields := fields ++ [("numerical_control", Lean.toJson true), ("output_words", Lean.toJson words)]
  | _ => throw (IO.userError "Supply graph JSON and at most one numerical request")
  IO.println ("TORCHLEAN_IMPORT_CHECK " ++ (Lean.Json.mkObj fields).compress)
