{ runCommand, python, snapshot, calibration }:
runCommand "tinystories-quantized" { nativeBuildInputs = [ python ]; } ''
  export HOME="$TMPDIR" OMP_NUM_THREADS=1
  mkdir "$out"
  python ${../../model/quantize.py} ${snapshot} ${calibration} "$out/model.pt2"
''
