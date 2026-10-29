{ calyx-ir, coreutils, memory, runCommand }:
runCommand "memory-contracts" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${memory}/bin/llm2fpga-calyx-opt' \
    '${calyx-ir}/calyx.mlirbc' \
    --mlir-print-debuginfo \
    --verify-each \
    --llm2fpga-complete-calyx-memory-contracts \
    --llm2fpga-resolve-runtime-memories \
    --emit-bytecode \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
