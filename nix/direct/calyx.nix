{ runCommand, memory, float-calyx, arithmetic }:
runCommand "tinystories-calyx" {} ''
  mkdir "$out"
  cp ${arithmetic}/model.mlir model.mlir
  ${memory}/bin/llm2fpga-calyx-opt model.mlir \
    --calyx-float-cast-source=${float-calyx}/share/calyx/primitives/float/float_cast.sv \
    --flatten-memref --canonicalize \
    --lower-scf-to-calyx="top-level-function=main write-json=memories" \
    --symbol-dce -o "$out/model.mlir"
  cp memories.json "$out/"
''
