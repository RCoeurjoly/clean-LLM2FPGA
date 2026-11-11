{ runCommand, yosys, dsp }:
runCommand "tinystories-ram" {} ''
  mkdir "$out"
  ${yosys}/bin/yosys -Q -p "
    read_rtlil ${dsp}/model.il;
    submod -name model_memories t:\$mem_v2;
    select *;
    memory -nomap;
    synth_xilinx -family xc7 -top model_top -noiopad -noclkbuf -run map_memory:map_ffram;
    flatten -noscopeinfo;
    check -assert;
    stat -tech xilinx;
    write_rtlil $out/model.il
  "
''
