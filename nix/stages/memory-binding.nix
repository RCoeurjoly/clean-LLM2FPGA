{ coreutils, memory, memory-validation, runCommand }:
runCommand "memory-binding" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${memory}/bin/llm2fpga-calyx-opt' \
    '${memory-validation}/calyx.mlirbc' \
    --verify-each \
    --mlir-print-debuginfo \
    --emit-bytecode \
    --llm2fpga-retire-memory-requests \
    '--llm2fpga-bind-model-memories=primitive-source=${../../snapshot/artifacts/task6/board-fit-2026-10-25/probe/lut-candidate-131072/closure_memory.sv} primitive-source-sha256=f0b54c1a5818ae72e146dc7030c5ecac4b6c6b159893fe3b1873053429ed95c5' \
    --llm2fpga-expose-runtime-memories \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
