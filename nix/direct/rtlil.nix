{ runCommand, yosys, rtl }:
runCommand "tinystories-rtlil" {} ''
  mkdir "$out"
  ${yosys}/bin/yosys -Q -p "
    read_verilog -sv ${rtl}/core.sv ${rtl}/model.sv ${rtl}/memory.sv;
    hierarchy -check -top model_top;
    proc;
    memory_collect;
    opt_dff *model_memory;
    select *;
    flatten -noscopeinfo;
    opt_clean;
    check -assert;
    setattr -set fsm_encoding \"none\" w:*;
    select *;
    write_rtlil $out/model.il
  "
''
