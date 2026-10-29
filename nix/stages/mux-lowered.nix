{ coreutils, divided-clock, mux-plugin, runCommand, yosys }:
runCommand "mux-lowered" {
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
    '${mux-plugin}/mux_to_lut.so' \
    -p \
    '
      read_rtlil ${divided-clock}/checkpoint.il.gz;
      write_json /dev/null;
      llm2fpga_mux_to_lut;
      check -assert;
      scc -expect 0;
      write_rtlil checkpoint.il.gz
    '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
