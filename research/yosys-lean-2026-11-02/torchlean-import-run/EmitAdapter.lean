import NN.Runtime.PyTorch.Export.TorchExport

def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply output adapter path")
  IO.FS.writeFile path (Export.PyTorch.TorchExport.script {})
  IO.println s!"TORCHLEAN_ADAPTER_EMITTED {path}"
