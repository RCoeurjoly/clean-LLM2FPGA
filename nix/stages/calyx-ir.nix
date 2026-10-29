{ coreutils, memory, reduced-index, runCommand }:
runCommand "calyx-ir" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${memory}/bin/llm2fpga-calyx-opt' \
    '${reduced-index}/shared-functions.mlir' \
    --mlir-print-debuginfo \
    --verify-each \
    --lower-scf-to-calyx=top-level-function=main \
    --symbol-dce \
    --emit-bytecode \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
