{ coreutils, memory, memory-inputs, runCommand, shared-logic }:
runCommand "memory-validation" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${memory}/bin/llm2fpga-calyx-opt' \
    '${shared-logic}/calyx.mlirbc' \
    --verify-each \
    --mlir-print-debuginfo \
    --emit-bytecode \
    '--llm2fpga-validate-memory-bindings=manifest=${memory-inputs}/bindings.json manifest-sha256=6fbf4550ae7144fc94198094b5e788c516beabd4850a8c7c1f625caf70b93a23' \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
