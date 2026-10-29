{ core, coreutils, design-ram, runCommand, yosys }:
runCommand "mapped" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  cp ${core}/input-*.hex .
  # write_json sorts the design; preserve that ordering without storing dumps.
  timeout 7200 ${yosys}/bin/yosys -p '
    read_rtlil "${design-ram}/checkpoint.il.gz";
    flatten -noscopeinfo main; select *; hierarchy -check -top main; opt_clean; opt;
    select main; write_json -selected /dev/null; select *;
    read_verilog -lib -specify +/xilinx/cells_sim.v;
    read_verilog -lib +/xilinx/cells_xtra.v;
    select main; write_json -selected /dev/null; select *;
    synth_xilinx -family xc7 -top main -noiopad -noclkbuf -nocarry -abc9 -run map_ffram:;
    flatten -noscopeinfo main; select *; hierarchy -check -top main; opt_clean;
    check -assert; scc -expect 0; write_rtlil checkpoint.il.gz
  '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
