{ core-coarse, coreutils, runCommand, yosys }:
runCommand "core-ram" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 7200 '${yosys}/bin/yosys' \
    -p \
    '
      read_rtlil "${core-coarse}/checkpoint.il.gz";
      synth_xilinx -family xc7 -top main -noiopad -noclkbuf -run map_memory:map_ffram;
      write_rtlil checkpoint.il.gz
    '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
