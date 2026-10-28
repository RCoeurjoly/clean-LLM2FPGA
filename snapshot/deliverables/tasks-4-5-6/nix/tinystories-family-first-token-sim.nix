{ pkgs, python, family, oracles, images, source }:
let
  mkSimulation = size:
    let
      wide = size == "8M" || size == "28M" || size == "33M";
      maxCycles = if size == "8M" then "2000000000"
        else if size == "28M" then "6000000000" else "7000000000";
      runTimeout = if size == "8M" then "14400" else "43200";
    in
    pkgs.runCommand "task5-tinystories-${size}-generated-rtl-first-token" {
      nativeBuildInputs = [ python pkgs.verilator pkgs.gnumake pkgs.gcc pkgs.coreutils ];
    } ''
      export PYTHONPATH=${source}
      export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
      mkdir -p "$out"
      ${if wide then ''
        python ${../scripts/pipeline/run_large_tinystories_family_sim.py} \
          --image ${images.${size}} --oracle ${oracles.${size}}/oracle.json \
          --output "$out" --max-cycles ${maxCycles} --run-timeout ${runTimeout}
      '' else pkgs.lib.removeSuffix "\n" ''
        python ${source}/scripts/pipeline/simulate_tinystories_family_first_token.py \
          --image ${images.${size}} --oracle ${oracles.${size}}/oracle.json \
          --output "$out" --max-cycles 2000000000
      ''}
      test -s "$out/trace.json"
      test -s "$out/result.json"
    '';
in builtins.listToAttrs (map (size: {
  name = size;
  value = mkSimulation size;
}) [ "1M" "3M" "8M" "28M" "33M" ])
