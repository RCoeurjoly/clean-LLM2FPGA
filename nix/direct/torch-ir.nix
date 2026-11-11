{ runCommand, python, torchMlir, quantized }:
let site = "${torchMlir}/lib/python3.11/site-packages";
in
runCommand "tinystories-torch-ir" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${site}:${site}/torch_mlir";
} ''
  export HOME="$TMPDIR" OMP_NUM_THREADS=1
  mkdir "$out"
  python ${../../model/import_torch.py} ${quantized}/model.pt2 "$out/model.mlir"
''
