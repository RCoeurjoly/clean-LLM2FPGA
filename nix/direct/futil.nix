{ runCommand, memory, calyx }:
runCommand "tinystories-futil" {} ''
  mkdir "$out"
  ${memory}/bin/llm2fpga-calyx-translate ${calyx}/model.mlir \
    --export-calyx -o "$out/model.futil"
''
