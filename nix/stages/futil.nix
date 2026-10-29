{ coreutils, external-interface, memory, runCommand }:
runCommand "futil" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${memory}/bin/llm2fpga-calyx-translate' \
    '${external-interface}/calyx.mlirbc' \
    --export-calyx \
    -o \
    kernel.futil
  test -s kernel.futil
  mkdir "$out"
  cp kernel.futil "$out/"
''
