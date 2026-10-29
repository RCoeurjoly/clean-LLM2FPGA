{ core, coreutils, rtl, runCommand, yosys }:
runCommand "pre-share" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  cp ${core}/input-*.hex .
  timeout 7200 '${yosys}/bin/yosys' \
    -p \
    '
      read_verilog -sv "${rtl}/kernel.sv";
      synth_xilinx -family xc7 -top main -noiopad -noclkbuf -run begin:begin;
      proc;
      opt_connection_helpers */t:*std_bit_slice*;
      select *;
      hierarchy -check -top main;
      opt_whole_wire_aliases;
      opt_clean -purge;
      flatten -noscopeinfo */t:*std_slt* */t:*std_sle* */t:*std_sgt* */t:*std_sge*;
      select *;
      hierarchy -check -top main;
      opt;
      synth_xilinx -family xc7 -top main -noiopad -noclkbuf -run prepare:coarse;
      techmap -map +/cmp2lut.v -map +/cmp2lcu.v -D LUT_WIDTH=6;
      alumacc;
      write_rtlil pre-share.il.gz
    '
  test -s pre-share.il.gz
  mkdir "$out"
  cp pre-share.il.gz "$out/"
''
