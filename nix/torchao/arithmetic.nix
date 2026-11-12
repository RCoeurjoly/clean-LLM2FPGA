{ runCommand, mlir, memory, loops }:
let base = import ../direct/arithmetic.nix { inherit runCommand mlir memory loops; };
in base.overrideAttrs (old: {
  name = "tinystories-torchao-arithmetic";
  # Arithmetic preparation expands memref.copy into loads/stores. Only then can
  # alias folding eliminate the final logits view before Calyx memory flattening.
  buildCommand = old.buildCommand + ''
    ${mlir}/bin/mlir-opt "$out/model.mlir" \
      --fold-memref-alias-ops --lower-affine --canonicalize --cse -o normalized.mlir
    mv normalized.mlir "$out/model.mlir"
  '';
})
