{ coreutils, memory, memory-binding, runCommand }:
runCommand "external-interface" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${memory}/bin/llm2fpga-calyx-opt' \
    '${memory-binding}/calyx.mlirbc' \
    --verify-each \
    --emit-bytecode \
    --llm2fpga-export-external-memory-interface=interface=interface.json \
    -o \
    calyx.mlirbc
  test -s calyx.mlirbc
  mkdir "$out"
  cp calyx.mlirbc "$out/"
''
