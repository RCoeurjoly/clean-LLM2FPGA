{ coreutils, runCommand, shared-functions, torch }:
runCommand "cse" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${torch}/bin/llm2fpga-torch-opt' \
    '${shared-functions}/shared-functions.mlir' \
    --verify-each \
    --cse \
    --canonicalize \
    -o \
    shared-functions.mlir
  test -s shared-functions.mlir
  mkdir "$out"
  cp shared-functions.mlir "$out/"
''
