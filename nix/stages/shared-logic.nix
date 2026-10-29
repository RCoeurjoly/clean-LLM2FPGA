{ coreutils, memory, memory-contracts, runCommand }:
runCommand "shared-logic" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${memory}/bin/llm2fpga-calyx-opt' \
    '${memory-contracts}/calyx.mlirbc' \
    --verify-each \
    --llm2fpga-share-register-bounded-comb \
    --emit-bytecode \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
