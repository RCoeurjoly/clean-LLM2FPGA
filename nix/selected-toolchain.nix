# Tool/model recipes retained from the selected tasks-4-5-6 flake.
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
    fourStateVerifier = verifier;
  };
  yosys = pkgs.callPackage (recipes + /graph-yosys.nix) {};
  calyxDriver = pkgs.callPackage (recipes + /graph-calyx-passes.nix) {
    inherit yosys circt;
    fourStateVerifier = verifier;
    baselineCalyx = calyx;
    calyx = backend;
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
  modelInputs = import ../source/upstream/nix/graph-core-data.nix {
    inherit (pkgs) runCommand;
    inherit snapshot;
    package = "${inputs.kev-gpt-src}/model_packages/tinystories-1m";
  };
  core = pkgs.callPackage ../source/upstream/nix/graph-core.nix {
    inherit python modelInputs circt;
    torchMlir = torch.torchMlir;
    torchDriver = torch.driver;
    memoryDriver = calyxDriver;
    calyx = backend;
    recoverRestoringSqrtLoops = true;
    coalescePointwisePingpong = true;
    fuseGemvEpilogues = true;
    poolPrivateCapacity = true;
    stableScratchPooling = true;
  };
  xc7 = inputs.openXC7.packages.${system};
  nextpnrSource = pkgs.fetchgit {
    name = "source";
    url = "https://github.com/RCoeurjoly/nextpnr-xilinx";
    rev = "32e8b798520d90e1130afe95908f04cf52125cf6";
    hash = "sha256-qKIxVFH1okPM1w2WFR0xgDisjRSw65qHGQpQa5eakgA=";
    fetchSubmodules = true;
  };
  chipdbNextpnr = xc7.nextpnr-xilinx.overrideAttrs (_: {
    src = nextpnrSource;
    patches = map (name: ../source/toolchain/patches/nextpnr-xilinx + "/${name}") [
      "0023-disconnect-lut6-a6-before-memory-constant.patch"
      "0024-drop-constant-a6-on-lut5.patch"
      "0037-router2-reserve-wire-index-storage.patch"
    ];
  });
  routeControl = chipdbNextpnr.overrideAttrs (_: {
    src = nextpnrSource;
    patches = [];
    postPatch = ''
      substituteInPlace common/placer1.cc \
        --replace-fail 'timing_analysis(ctx);' \
        'timing_analysis(ctx, true, true, true, false);'
    '';
  });
  geometry = routeControl.overrideAttrs (old: {
    pname = "nextpnr-xilinx-kintex-geometry-diagnostic";
    patches = (old.patches or []) ++ [
      ../source/toolchain/patches/nextpnr-xilinx/0038-allow-kintex-heap-geometry-diagnostic-overrides.patch
    ];
  });
  checkpoint = geometry.overrideAttrs (old: {
    pname = "nextpnr-xilinx-kintex-checkpoint-diagnostic";
    patches = (old.patches or []) ++ map (name: ../source/toolchain/patches/nextpnr-xilinx + "/${name}") [
      "0040-import-emitted-wire-to-wire-pip-names.patch"
      "0041-preserve-unique-pip-identity-in-checkpoints.patch"
      "0042-normalize-evidenced-legacy-slice-clock-selectors.patch"
      "0043-recover-canonical-int-pip-aliases-from-original-fasm.patch"
      "0044-ignore-functionally-unused-postroute-lut-input-arcs.patch"
    ];
  });
  nextpnr = checkpoint.overrideAttrs (old: {
    pname = "nextpnr-xilinx-kintex-bram-mode-fix";
    patches = (old.patches or []) ++ [
      ../source/toolchain/patches/nextpnr-xilinx/0045-emit-ramb36-odd-width-mode-features.patch
    ];
  });
  chipdb = xc7.nextpnr-xilinx-chipdb.kintex7.override {
    chipdbFootprints = [ "xc7k480tffg1156" ];
    "nextpnr-xilinx" = chipdbNextpnr;
  };
  bitstreamPkgs = import inputs.bitstream-nixpkgs { inherit system; };
  pythonDeps = bitstreamPkgs.python312.withPackages (ps: [
    ps.pyyaml ps.simplejson ps.intervaltree ps.pyjson5 ps.progressbar2
  ]);
  patchedDb = pkgs.runCommand "task6-bram-prjxray-db-lioi3-compat" {
    nativeBuildInputs = [ pkgs.patch ];
  } ''
    set -euo pipefail
    original=${nextpnr}/share/nextpnr/external/prjxray-db
    echo "d74bb512c56af57510ce89b351f1bf666e96196e20c8b1bc18e8521fe45bdd61  $original/kintex7/segbits_lioi3_tbytesrc.db" | sha256sum -c -
    echo '197e2f126a6f3917c1ceca62394acd957397679538df077f7119de97c28e05bc  ${../source/toolchain/patches/prjxray-db/0001-kintex7-add-lioi3-tbytesrc-oclkm-imux31.patch}' | sha256sum -c -
    cp -rs "$original" "$out"
    chmod u+w "$out/kintex7"
    cp --remove-destination "$original/kintex7/segbits_lioi3_tbytesrc.db" "$out/kintex7/segbits_lioi3_tbytesrc.db"
    chmod u+w "$out/kintex7/segbits_lioi3_tbytesrc.db"
    cd "$out"
    patch --no-backup-if-mismatch -p1 < ${../source/toolchain/patches/prjxray-db/0001-kintex7-add-lioi3-tbytesrc-oclkm-imux31.patch}
    echo '7108c7c92097d591e404852ce5eb18a6ed1851352d399eb29c1bd7eed3ffd710  kintex7/segbits_lioi3_tbytesrc.db' | sha256sum -c -
  '';
in {
  graph-torch-opt = torch.driver;
  graph-torch-mlir = torch.torchMlir;
  graph-torch-mlir-sdk = torch.mlir;
  graph-circt = circt;
  graph-calyx-opt = calyxDriver;
  graph-calyx-backend = backend;
  graph-yosys = yosys;
  graph-core-1m-stable-scratch = core;
  model-snapshot = modelInputs.snapshot;
  model-package = modelInputs.package;
  nextpnr-xilinx = nextpnr;
  nextpnr-xilinx-chipdb = chipdb;
  prjxray = xc7.prjxray;
  fasm = xc7.fasm;
  prjxray-python = pythonDeps;
  prjxray-db = patchedDb;
}
