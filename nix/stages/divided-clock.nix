{ clock-plugin, coreutils, mapped, runCommand, yosys }:
runCommand "divided-clock" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  # write_json sorts the design; preserve that ordering without storing dumps.
  timeout 1800 '${yosys}/bin/yosys' \
    -m \
    '${clock-plugin}/clock_divide2.so' \
    -p \
    '
      read_rtlil "${mapped}/checkpoint.il.gz";
      select main;
      write_json -selected /dev/null;
      select *;
      llm2fpga_clock_divide2 -buffered;
      check -assert;
      scc -expect 0;
      select main;
      write_json -selected /dev/null;
      select *;
      write_rtlil checkpoint.il.gz
    '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
