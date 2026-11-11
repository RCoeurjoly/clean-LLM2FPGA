{ runCommand, yosys, prepared }:
runCommand "tinystories-dsp" {} ''
  mkdir "$out"
  ${yosys}/bin/yosys -Q -p "
    read_rtlil ${prepared}/model.il;
    synth_xilinx -family xc7 -top model_top -noiopad -noclkbuf -run begin:prepare;
    synth_xilinx -family xc7 -top model_top -noiopad -noclkbuf -run map_dsp:coarse;
    opt_clean;
    check -assert;
    stat -tech xilinx;
    write_rtlil $out/model.il
  "
''
