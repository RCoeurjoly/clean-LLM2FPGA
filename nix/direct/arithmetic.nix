{ runCommand, mlir, memory, loops }:
runCommand "tinystories-arithmetic" {} ''
  mkdir "$out"
  ${memory}/bin/llm2fpga-calyx-opt ${loops}/model.mlir \
    --prepare-calyx-math --prepare-calyx-arithmetic -o prepared.mlir
  ${mlir}/bin/mlir-opt prepared.mlir \
    --arith-expand --canonicalize --cse -o "$out/model.mlir"
''
