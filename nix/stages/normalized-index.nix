{ coreutils, flat-memory, runCommand, torch }:
runCommand "normalized-index" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${torch}/bin/llm2fpga-torch-opt' \
    '${flat-memory}/flat-memory.mlir' \
    --mlir-print-debuginfo \
    --verify-each \
    --llm2fpga-normalize-index-scales-for-sharing \
    -o \
    flat-memory.mlir
  test -s flat-memory.mlir
  mkdir "$out"
  cp flat-memory.mlir "$out/"
''
