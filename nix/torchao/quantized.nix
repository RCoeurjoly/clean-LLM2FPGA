{ lib, runCommand, python, snapshot, calibration }:
let source = lib.fileset.toSource {
  root = ../..;
  fileset = lib.fileset.unions [
    ../../model/quantize.py ../../model/quantize_torchao.py
  ];
};
in runCommand "tinystories-torchao-quantized" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = source;
} ''
  export HOME="$TMPDIR" OMP_NUM_THREADS=1
  mkdir "$out"
  python ${source}/model/quantize_torchao.py ${snapshot} ${calibration} "$out/model.pt2"
''
