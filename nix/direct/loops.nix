{ runCommand, mlir, linalg }:
runCommand "tinystories-loops" {} ''
  mkdir "$out"
  ${mlir}/bin/mlir-opt ${linalg}/model.mlir \
    --empty-tensor-to-alloc-tensor \
    --one-shot-bufferize="bufferize-function-boundaries \
      function-boundary-type-conversion=identity-layout-map" \
    --buffer-results-to-out-params="modify-public-functions hoist-static-allocs" \
    --convert-bufferization-to-memref \
    --convert-linalg-to-loops --fold-memref-alias-ops --lower-affine \
    --canonicalize --cse -o "$out/model.mlir"
''
