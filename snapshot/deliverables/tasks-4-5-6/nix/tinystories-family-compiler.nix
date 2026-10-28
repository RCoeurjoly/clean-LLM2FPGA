{ pkgs, python, calyx, family, packages, oracles, source }:
let
  mkCompiler = size:
    let model = family.${size};
    in pkgs.runCommand "task5-tinystories-${size}-generated-sv" {
      nativeBuildInputs = [ python pkgs.coreutils ];
    } ''
      export PYTHONPATH=${source}
      export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
      mkdir -p "$out"
      timeout 1800 python ${source}/scripts/pipeline/build_tinystories_family_compiler.py \
        --snapshot ${model.snapshot} --package ${packages.${size}} \
        --revision ${model.revision} --oracle ${oracles.${size}}/oracle.json \
        --calyx ${calyx}/bin/calyx --calyx-library ${calyx}/share/calyx \
        --size ${size} --output "$out"
      test -s "$out/model.futil"
      test -s "$out/model.sv"
      test -s "$out/build.json"
    '';
in builtins.listToAttrs (map (size: {
  name = size;
  value = mkCompiler size;
}) [ "1M" "3M" "8M" "28M" "33M" ])
