{ pkgs, python, yosys, slang, images, simulations }:
let
  mkSynthesis = size:
    pkgs.runCommand "task5-tinystories-${size}-bounded-xc7-synthesis" {
      nativeBuildInputs = [ python yosys slang pkgs.coreutils ];
    } ''
      export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
      ulimit -v 8388608
      python ${../scripts/pipeline/synthesize_tinystories_family.py} \
        --image ${images.${size}} --simulation ${simulations.${size}} \
        --output "$out" --size ${size} --yosys ${yosys} --slang ${slang} \
        --timeout-seconds ${if size == "1M" then "3600" else "28800"}
      test -s "$out/result.json"
    '';
in builtins.listToAttrs (map (size: {
  name = size;
  value = mkSynthesis size;
}) [ "1M" "3M" "8M" "28M" "33M" ])
