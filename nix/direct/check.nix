{ lib, runCommand, python, snapshot, calibration }:
let
  source = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      ../../model/quantize.py ../../model/import_torch.py ../../tests/test_quantize.py
    ];
  };
in runCommand "tinystories-quantization-check" {
  nativeBuildInputs = [ python ];
  PYTHONPATH = source;
  PYTHONDONTWRITEBYTECODE = "1";
  MODEL_SNAPSHOT = snapshot;
  CALIBRATION = calibration;
} ''
  export HOME="$TMPDIR" OMP_NUM_THREADS=1
  python -m unittest discover -s ${source}/tests -p test_quantize.py -v
  touch "$out"
''
