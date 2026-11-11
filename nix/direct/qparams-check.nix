{ lib, runCommand, python, torchMlir, mlir, quantized }:
let
  source = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../model/import_torch.py ../../tests/extract_operation.py
      ../../tests/test_qparams.py
    ];
  };
  site = "${torchMlir}/lib/python3.11/site-packages";
in runCommand "model-qparams-equivalence" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = "${source}:${source}/tests:${site}:${site}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  QUANTIZED_MODEL = "${quantized}/model.pt2";
  TORCH_OPT = "${torchMlir}/bin/torch-mlir-opt";
  MLIR_OPT = "${mlir}/bin/mlir-opt";
} ''
  export HOME="$TMPDIR" OMP_NUM_THREADS=1
  python -m unittest discover -s ${source}/tests -p test_qparams.py -v
  touch "$out"
''
