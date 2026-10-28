{ pkgs, python, family, packages, oracles, compiler, source }:
let
  mkImage = size:
    let model = family.${size};
    in pkgs.runCommand "task5-tinystories-${size}-static-image" {
      nativeBuildInputs = [ python pkgs.coreutils ];
    } ''
      export PYTHONPATH=${source}
      export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
      mkdir -p "$out"
      timeout 900 python ${source}/scripts/pipeline/build_tinystories_family_image.py \
        --snapshot ${model.snapshot} --package ${packages.${size}} \
        --revision ${model.revision} --oracle ${oracles.${size}}/oracle.json \
        --compiler ${compiler.${size}} --size ${size} --output "$out"
      test -s "$out/model-static-init-normalized.sv"
      test -s "$out/image.json"
    '';
in builtins.listToAttrs (map (size: {
  name = size;
  value = mkImage size;
}) [ "1M" "3M" "8M" "28M" "33M" ])
