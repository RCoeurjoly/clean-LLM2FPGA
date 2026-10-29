{ coreutils, cse, runCommand, torch }:
runCommand "narrowed-loops" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${torch}/bin/llm2fpga-torch-opt' \
    '${cse}/shared-functions.mlir' \
    --verify-each \
    --arith-int-range-narrowing=int-bitwidths-supported=8,16,32,64 \
    --scf-for-to-while \
    --canonicalize \
    -o \
    shared-functions.mlir
  test -s shared-functions.mlir
  mkdir "$out"
  cp shared-functions.mlir "$out/"
''
