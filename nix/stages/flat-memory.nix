{ coreutils, memory, runCommand, structured }:
runCommand "flat-memory" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${memory}/bin/llm2fpga-calyx-opt' \
    '${structured}/structured.mlir' \
    --mlir-print-debuginfo \
    --verify-each \
    --flatten-memref \
    --canonicalize \
    -o \
    flat-memory.mlir
  test -s flat-memory.mlir
  mkdir "$out"
  cp flat-memory.mlir "$out/"
''
