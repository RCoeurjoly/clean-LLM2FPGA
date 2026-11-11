{ runCommand, torchMlir, torch-ir }:
runCommand "tinystories-linalg" {} ''
  mkdir "$out"
  ${torchMlir}/bin/torch-mlir-opt ${torch-ir}/model.mlir \
    --torchdynamo-export-to-torch-backend-pipeline \
    --torch-backend-to-linalg-on-tensors-backend-pipeline \
    --canonicalize -o "$out/model.mlir"
''
