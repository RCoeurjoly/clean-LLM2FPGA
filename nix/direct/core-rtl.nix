{ runCommand, float-calyx, futil }:
runCommand "tinystories-core-rtl" {} ''
  mkdir "$out"
  ${float-calyx}/bin/calyx ${futil}/model.futil \
    -l ${float-calyx}/share/calyx -p external --synthesis \
    -x cell-share:bounds=1,-1,-1 \
    -b verilog -o "$out/core.sv"
''
