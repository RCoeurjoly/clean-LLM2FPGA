{ core, core-ram, coreutils, runCommand, yosys }:
runCommand "design-coarse" {
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
      read_rtlil "${core-ram}/checkpoint.il.gz";
      rename main graph_native_main;
      read_verilog -sv "${../../snapshot/artifacts/task6/board-fit-2026-10-25/probe/lut-candidate-131072/closure_memory.sv}" "${../../snapshot/deliverables/tasks-4-5-6/rtl/graph_onchip_runtime.sv}" "${../../snapshot/deliverables/tasks-4-5-6/rtl/graph_tinystories_ypcb_top.sv}" "${../../snapshot/deliverables/tasks-4-5-6/rtl/tinystories_1m_jtag_csr.sv}" "${../../snapshot/deliverables/tasks-4-5-6/rtl/tinystories_1m_ypcb_clock.sv}";
      rename graph_tinystories_ypcb_top main;
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
      share -fast -pattern-limit 64;
      opt;
      memory -nomap;
      opt_clean;
      iopadmap -bits -inpad IBUF O:I -outpad OBUF I:O main;
      flatten -noscopeinfo main;
      select *;
      hierarchy -check -top main;
      opt_clean;
      simplemap;
      opt_clean;
      check -assert;
      write_rtlil checkpoint.il.gz
    '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
