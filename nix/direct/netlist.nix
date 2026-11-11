{ runCommand, yosys, ram }:
runCommand "tinystories-netlist" {} ''
  mkdir "$out"
  ${yosys}/bin/yosys -Q -p "
    read_rtlil ${ram}/model.il;
    blackbox =A:whitebox;
    write_json $out/model.json
  "
''
