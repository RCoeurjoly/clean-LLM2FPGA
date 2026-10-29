{ coreutils, narrowed-loops, runCommand, torch }:
runCommand "reduced-index" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${torch}/bin/llm2fpga-torch-opt' \
    '${narrowed-loops}/shared-functions.mlir' \
    --mlir-print-debuginfo \
    --verify-each \
    --llm2fpga-strength-reduce-integer-constants=index-scales=true \
    --canonicalize \
    -o \
    shared-functions.mlir
  test -s shared-functions.mlir
  mkdir "$out"
  cp shared-functions.mlir "$out/"
''
