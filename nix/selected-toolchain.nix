{ inputs, system }:
let
  pkgs = import inputs.nixpkgs { inherit system; };
  pkgsLlvm21 = import inputs.nixpkgs-llvm21 {
    inherit system;
    config.allowUnfreePredicate = pkg: builtins.elem (pkgs.lib.getName pkg) [ "torch" ];
  };
  python = pkgsLlvm21.python311;
  recipes = ../source/toolchain/nix;
  torch = import (recipes + /graph-toolchain.nix) { pkgs = pkgsLlvm21; inherit python; };
  circtPackages = inputs.graph-circt-nix.packages.${system};
  circt = (circtPackages.circt.override { enableSlang = false; }).overrideAttrs (old: {
    patches = (old.patches or []) ++ [
      ../source/toolchain/nix/patches/graph-calyx-control-symbol-verification.patch
      ../source/toolchain/nix/patches/graph-calyx-group-use-verification.patch
    ];
  });
  hardfloat = pkgsLlvm21.callPackage (recipes + /hardfloat.nix) {};
  calyx = pkgsLlvm21.callPackage (recipes + /calyx.nix) {
    calyxSrc = inputs.calyx-src;
    inherit hardfloat;
  };
  verifier = pkgs.callPackage (recipes + /graph-four-state-verifier.nix) {};
  backend = pkgsLlvm21.callPackage (recipes + /graph-calyx-backend.nix) {
    inherit calyx;
  };
  yosys = pkgs.callPackage (recipes + /graph-yosys.nix) {};
  calyxDriver = pkgs.callPackage (recipes + /graph-calyx-passes.nix) {
    inherit circt;
    circtSource = inputs.graph-circt-src;
    inherit (circtPackages) mlir libllvm;
  };
  fetchModelFile = file: hash: pkgs.fetchurl {
    url = "https://huggingface.co/roneneldan/TinyStories-1M/resolve/ac533fb8b4f69c71894bf96badfe11e6294d9fcf/${file}";
    inherit hash;
  };
  snapshot = pkgs.linkFarm "tinystories-1m-hf-snapshot" [
    { name = "config.json"; path = fetchModelFile "config.json"
        "sha256-/3TDDV67WrHaDy6kea33GXxQS0K1UiqFjDNKuR7UlYw="; }
    { name = "pytorch_model.bin"; path = fetchModelFile "pytorch_model.bin"
        "sha256-B/lgnqiCuBY/87I9QOK4LLcV1AljG+sVyEsWTzh32uc="; }
  ];
in {
  graph-torch-mlir = torch.torchMlir;
  graph-torch-mlir-sdk = torch.mlir;
  graph-circt = circt;
  graph-calyx-opt = calyxDriver;
  graph-calyx-backend = backend;
  graph-yosys = yosys;
  graph-four-state-verifier = verifier;
  model-snapshot = pkgs.runCommand "graph-core-model-snapshot" {} ''
    mkdir -p "$out"
    cp ${snapshot}/config.json "$out/config.json"
    cp ${snapshot}/pytorch_model.bin "$out/pytorch_model.bin"
  '';
}
