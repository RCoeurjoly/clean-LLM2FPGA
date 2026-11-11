{ runCommand, yosys, rtlil }:
runCommand "tinystories-prepared" {} ''
  mkdir "$out"
  ${yosys}/bin/yosys -Q -p "
    read_rtlil ${rtlil}/model.il;
    prep -top model_top;
    check -assert;
    write_rtlil $out/model.il
  "
''
