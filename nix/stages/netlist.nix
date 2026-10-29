{ coreutils, mux-lowered, runCommand, yosys }:
runCommand "netlist" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${yosys}/bin/yosys' \
    -p \
    '
      read_rtlil "${mux-lowered}/checkpoint.il.gz";
      check -assert;
      scc -expect 0;
      write_json mapped.json.gz;

    '
  test -s mapped.json.gz
  mkdir "$out"
  cp mapped.json.gz "$out/"
''
