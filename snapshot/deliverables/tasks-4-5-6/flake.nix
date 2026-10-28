{
  description = "LLM2FPGA Tasks 6, 4, and 5 curated TinyStories deliverable";

  inputs = {
    graph-ypcb-src = {
      url = "github:RCoeurjoly/ypcb_00338_1p1_hack/8b13ecf71b7061dd43a2b1580f0caa469f64303f";
      flake = false;
    };
    nixpkgs.url =
      "github:NixOS/nixpkgs/6fd329b2adfecb86ae49c1cba89689bd0f229e04";
    nixpkgs-llvm21.url =
      "github:NixOS/nixpkgs/346dd96ad74dc4457a9db9de4f4f57dab2e5731d";
    nixpkgs-nix-eda.url =
      "github:NixOS/nixpkgs/50ab793786d9de88ee30ec4e4c24fb4236fc2674";
    bitstream-nixpkgs.url =
      "github:NixOS/nixpkgs/6fd329b2adfecb86ae49c1cba89689bd0f229e04";
    flake-utils.url =
      "github:numtide/flake-utils/11707dc2f618dd54ca8739b309ec4fc024de578b";
    graph-circt-src = {
      url = "github:llvm/circt/5dc62fe46c9dbf8936f4f706083301e7503715eb";
      flake = false;
    };
    graph-circt-llvm-src = {
      url = "github:llvm/llvm-project/7cb3005ba22899b8ab7efe4fe43cba09cb7e12d4";
      flake = false;
    };
    graph-circt-nix = {
      url = "git+https://github.com/dtzSiFive/circt-nix?rev=305a7a3e8bf95e3770db5391ed0f3e9a6a0078c2";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.flake-utils.follows = "flake-utils";
      inputs.circt-src.follows = "graph-circt-src";
      inputs.llvm-submodule-src.follows = "graph-circt-llvm-src";
    };
    nix-eda = {
      url = "github:fossi-foundation/nix-eda/ebee72ea782641c04291dfa4cdcb560d22047bab";
      inputs.nixpkgs.follows = "nixpkgs-nix-eda";
    };
    openXC7.url =
      "github:RCoeurjoly/toolchain-nix/aebe5a3cf24ddd84818fb9a96daa96b8b76c796b";
    calyx-src = {
      url = "github:calyxir/calyx/5a4303847392609cad83dda6f4bdffc8cc0e5c89";
      flake = false;
    };
    graph-litedram-src = {
      url = "github:enjoy-digital/litedram/8bc71e8d006de7c83f01391b37633f270ecbae81";
      flake = false;
    };
    graph-litex-src = {
      url = "github:enjoy-digital/litex/328da97ab916fca166ada2f943ae08737a1bb277";
      flake = false;
    };
    kev-gpt-src = {
      url = "github:RCoeurjoly/kev-gpt/df1fc45b2ffcb26fddc19cfd57621e7eedf6153f";
      flake = false;
    };
  };

  outputs = inputs@{ nixpkgs, nixpkgs-llvm21, nix-eda, openXC7, flake-utils,
    kev-gpt-src, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        pkgsLlvm21 = import nixpkgs-llvm21 {
          inherit system;
          config.allowUnfreePredicate = pkg:
            builtins.elem (nixpkgs.lib.getName pkg) [ "torch" ];
        };
        python = pkgsLlvm21.python311;
        graphLiteDram = import ./nix/graph-litedram.nix {
          pkgs = pkgsLlvm21;
          litexSource = inputs.graph-litex-src;
          litedramSource = inputs.graph-litedram-src;
        };
        graphToolchain = import ./nix/graph-toolchain.nix {
          pkgs = pkgsLlvm21;
          inherit python;
        };
        graphCalyxPackages = inputs.graph-circt-nix.packages.${system};
        graphCirct = (graphCalyxPackages.circt.override { enableSlang = false; }).overrideAttrs (old: {
          patches = (old.patches or []) ++ [
            ./nix/patches/graph-calyx-control-symbol-verification.patch
            ./nix/patches/graph-calyx-group-use-verification.patch
          ];
        });
        graphCalyxBackend = pkgsLlvm21.callPackage ./nix/graph-calyx-backend.nix {
          inherit calyx;
          fourStateVerifier = graphFourStateVerifier;
        };
        graphFourStateVerifier = pkgs.callPackage ./nix/graph-four-state-verifier.nix {};
        graphYosys = pkgs.callPackage ./nix/graph-yosys.nix {};
        graphCalyxDriver = pkgs.callPackage ./nix/graph-calyx-passes.nix {
          yosys = graphYosys;
          fourStateVerifier = graphFourStateVerifier;
          baselineCalyx = calyx;
          calyx = graphCalyxBackend;
          circt = graphCirct;
          circtSource = inputs.graph-circt-src;
          inherit (graphCalyxPackages) mlir libllvm;
        };
        graphIntegerBoundary = pkgs.callPackage ./nix/graph-integer-boundary.nix {
          inherit (graphToolchain) mlir;
          torchDriver = graphToolchain.driver;
        };
        graphProducerSlices = pkgs.callPackage ./nix/graph-producer-slices.nix {
          inherit python;
          inherit (graphToolchain) mlir;
          torchDriver = graphToolchain.driver;
        };
        graphPointwiseFusion = pkgs.callPackage ./nix/graph-pointwise-fusion.nix {
          inherit python;
          inherit (graphToolchain) mlir torchMlir;
          torchDriver = graphToolchain.driver;
        };
        graphOneTripLoops = pkgs.callPackage ./nix/graph-one-trip-loops.nix {
          inherit python;
          inherit (graphToolchain) torchMlir;
          torchDriver = graphToolchain.driver;
        };
        graphHorizontalFusion = pkgs.callPackage ./nix/graph-horizontal-fusion.nix {
          inherit python;
          inherit (graphToolchain) torchMlir;
          torchDriver = graphToolchain.driver;
        };
        graphGenerationLink = pkgs.callPackage ./nix/graph-generation-link.nix {
          inherit python;
          inherit (graphToolchain) torchMlir;
          torchDriver = graphToolchain.driver;
          circt = graphCirct;
          memoryDriver = graphCalyxDriver;
        };
        graphLiteDramExportCheck = pkgs.callPackage ./nix/graph-litedram-export-check.nix {
          yosys = graphYosys;
          litedramPython = graphLiteDram.python;
        };
        graphBoardController = pkgs.callPackage ./nix/graph-board-controller.nix {
          litedramPython = graphLiteDram.python;
          boardSource = inputs.graph-ypcb-src;
        };
        graphCoreSourceCheck = pkgs.callPackage ./nix/graph-core-source-check.nix {
          inherit python;
        };
        graphCoreInputs = import ./nix/graph-core-data.nix {
          inherit (pkgs) runCommand;
          inherit snapshot;
          package = exactPackage;
        };
        graphCoreInputCheck = pkgs.callPackage ./nix/graph-core-input-check.nix {
          inherit python;
          modelInputs = graphCoreInputs;
        };
        graphCore1m = pkgs.callPackage ./nix/graph-core.nix {
          inherit python;
          torchMlir = graphToolchain.torchMlir;
          torchDriver = graphToolchain.driver;
          circt = graphCirct;
          memoryDriver = graphCalyxDriver;
          calyx = graphCalyxBackend;
          modelInputs = graphCoreInputs;
        };
        # Same authenticated graph compiler, with immutable tensors bound to
        # the dynamic DDR read interface. Transport/PHY are downstream stages.
        graphCoreExact1m = graphCore1m.override {
          recoverRestoringSqrtLoops = true;
          coalescePointwisePingpong = true;
        };
        graphCoreMemory1m = graphCoreExact1m.override {
          fuseGemvEpilogues = true; poolPrivateCapacity = true;
        };
        graphCoreMemorySynthesis = graphCoreSynthesis.override { core = graphCoreMemory1m; };
        graphCoreStableScratch1m = graphCoreMemory1m.override { stableScratchPooling = true; };
        graphCoreStableScratchSynthesis = graphCoreSynthesis.override { core = graphCoreStableScratch1m; };
        graphCoreExactSynthesis = graphCoreSynthesis.override { core = graphCoreExact1m; };
        graphCoreDdr1m = graphCore1m.override {
          narrowImmutable = false;
          offchipImmutable = true;
        };
        graphCoreSynthesis = pkgs.callPackage ./nix/graph-core-synthesis.nix {
          inherit python;
          core = graphCore1m;
          calyx = graphCalyxBackend;
        };
        graphCoreDdrSynthesis = graphCoreSynthesis.override {
          core = graphCoreDdr1m;
        };
        graphCoreDdrCoarse = pkgs.callPackage ./nix/graph-core-xc7-coarse.nix {
          inherit python;
          synthesis = graphCoreDdrSynthesis;
          yosys = graphYosys;
        };
        graphCoreDdrRam = pkgs.callPackage ./nix/graph-core-xc7-ram.nix {
          inherit python;
          coarse = graphCoreDdrCoarse;
          yosys = graphYosys;
        };
        graphCoreDdrFinal = pkgs.callPackage ./nix/graph-core-xc7-final.nix {
          inherit python;
          ram = graphCoreDdrRam;
          yosys = graphYosys;
        };
        graphCoreDdrTransport = pkgs.callPackage ./nix/graph-core-ddr-transport.nix {
          yosys = graphYosys;
          inherit python;
          torchMlir = graphToolchain.torchMlir;
          core = graphCoreDdr1m;
          controller = graphBoardController;
          calyx = graphCalyxBackend;
        };
        graphRuntimeVerifier = pkgs.callPackage ./nix/graph-runtime-verifier.nix {};
        graphNarrowRom = pkgs.callPackage ./nix/graph-narrow-rom.nix {
          yosys = graphYosys;
          memoryDriver = graphCalyxDriver;
          calyx = graphCalyxBackend;
        };
        graphMemoryReductions = pkgs.callPackage ./nix/graph-memory-reductions.nix {
          inherit python;
          inherit (graphToolchain) torchMlir;
          torchDriver = graphToolchain.driver;
          circt = graphCirct;
          memoryDriver = graphCalyxDriver;
          calyx = graphCalyxBackend;
        };
        graphStableScratchRtl = graphMemoryReductions.override { stableScratchPooling = true; };
        graphLutMemoryRtl = graphStableScratchRtl.override { lutMemoryMaxBits = 4096; };
        graphLutMemoryFullWidthRtl = graphLutMemoryRtl.override { fullWidthLutRoms = true; };
        graphExactReductions = pkgs.callPackage ./nix/graph-exact-reductions.nix {
          inherit python;
          torchDriver = graphToolchain.driver;
          memoryDriver = graphCalyxDriver;
          calyx = graphCalyxBackend;
        };
        graphKernelRtl = pkgs.callPackage ./nix/graph-pipeline.nix {
          yosys = graphYosys;
          inherit python;
          calyx = graphCalyxBackend;
          torchMlir = graphToolchain.torchMlir;
          torchDriver = graphToolchain.driver;
          circt = graphCirct;
          memoryDriver = graphCalyxDriver;
          task2Source = ./model_inputs/task2/matmul.py;
        };
        torchao = python.pkgs.buildPythonPackage rec {
          pname = "torchao";
          version = "0.15.0";
          format = "wheel";
          src = pkgs.fetchurl {
            url = "https://files.pythonhosted.org/packages/f6/3b/6b9d5618720f63dbc2e2509cd6b57aae9c0d61b738d1d2172f4d5d9efaab/torchao-0.15.0-py3-none-any.whl";
            hash = "sha256-PzgSZ2BI74oqDp1JLRLYlxunp+uxb1SqVvaQQU4TDSw=";
          };
          propagatedBuildInputs = [ python.pkgs.torch ];
          dontBuild = true;
          doCheck = false;
          pythonImportsCheck = [ "torchao" ];
        };
        pythonWithTinyStoriesTorchAO = python.withPackages (ps:
          [ ps.torch ps.packaging ps.transformers torchao ]);
        hardfloat = pkgsLlvm21.callPackage ./nix/hardfloat.nix { };
        calyx = pkgsLlvm21.callPackage ./nix/calyx.nix {
          calyxSrc = inputs.calyx-src;
          inherit hardfloat;
        };
        nixEdaPkgs = nix-eda.packages.${system};
        yosys = nixEdaPkgs.yosys.overrideAttrs (old: {
          pname = "yosys-dspfix2";
          enableParallelBuilding = false;
          patches = (old.patches or [ ]) ++ [
            ./patches/yosys/0001-xilinx-dsp-initialize-pattern-detect-parameter.patch
          ];
        });
        slang = nixEdaPkgs.yosys-slang;
        familySlang = slang.overrideAttrs (old: {
          postPatch = (old.postPatch or "") + ''
            patch -p1 < ${./nix/patches/yosys-slang-inferred-memory-initial-state.patch}
          '';
        });
        compilerSource = builtins.path {
          path = ./.;
          name = "task6-compiler-source";
          filter = path: type:
            let rel = pkgs.lib.removePrefix "${toString ./.}/" (toString path);
            in path == toString ./. || builtins.elem rel ([
              "scripts" "scripts/pipeline" "scripts/comparison"
              "TinyStories" "artifacts" "artifacts/reference" "tests"
              "docs" "docs/superpowers" "docs/superpowers/specs" "rtl"
              "tests/test_tinystories_1m_exact_generation.py"
              "docs/superpowers/specs/2026-08-28-reference-guided-tinystories-1m-compiler-design.md"
              "rtl/tinystories_1m_ypcb_clock.sv"
              "rtl/tinystories_1m_jtag_csr.sv"
              "rtl/tinystories_1m_ypcb_bram_production_top.sv"
            ] ++ map (name: "scripts/pipeline/${name}") [
              "calyx_program.py"
              "calyx_passes.py"
              "calyx_validation.py"
              "fix_sv_synthesis_frontend.py"
              "lower_fixed_point_attention_crossing_to_calyx.py"
              "lower_fixed_point_block_composition_to_calyx.py"
              "lower_fixed_point_mlp_crossing_to_calyx.py"
              "lower_fixed_point_model_bram_production.py"
              "lower_fixed_point_model_to_calyx.py"
              "lower_fixed_point_schema_to_calyx.py"
              "lower_fixed_point_token_step_to_calyx.py"
              "map_flattened_memory_checkpoints.py"
              "prepare_tinystories_1m_bram_image.py"
              "repair_calyx_signed_multiply_width.py"
              "rom_lutrams_xc7.txt"
              "run_bram_production_sv.py"
              "task6_two_rom_mapper.py"
              "tinystories_family_memory_plan.py"
              "tinystories_family_spec.py"
              "tinystories_generation_geometry.py"
              "validate_tinystories_stage.py"
            ] ++ map (name: "scripts/comparison/${name}") [
              "audit_tinystories_1m_exact_input.py"
              "capture_tinystories_1m_fixed_logits.py"
              "certify_tinystories_1m_exact_reachable_domain.py"
              "verify_tinystories_1m_exact_generation.py"
              "verify_closure_reference.py"
            ] ++ map (name: "TinyStories/${name}") [
              "capture_fixed_point_attention_crossing_slice.py"
              "capture_fixed_point_block_composition_slice.py"
              "capture_fixed_point_continuation_oracle.py"
              "capture_fixed_point_gemv_requantize_slice.py"
              "capture_fixed_point_mlp_crossing_slice.py"
              "capture_fixed_point_model_token_step_oracle.py"
              "model_adapter_exact_package.py"
              "model_adapter_reference_package.py"
              "serial_gemv_boundary.py"
            ] ++ map (name: "artifacts/reference/${name}") [
              "tinystories-1m-exact-generation.json"
              "tinystories-1m-exact-input-audit.json"
              "tinystories-1m-exact-input-contract.json"
              "tinystories-1m-exact-package-model.json"
              "tinystories-1m-exact-reachable-domain.json"
              "tinystories-1m-export-provenance.json"
              "tinystories-1m-fixed-hardware-qdq-profile.json"
              "tinystories-1m-fixed-logits-oracle.json"
              "tinystories-1m-fixed-point-attention-crossing-slice.json"
              "tinystories-1m-fixed-point-continuation-oracle.json"
              "tinystories-1m-fixed-point-mlp-crossing-slice.json"
              "tinystories-1m-fixed-point-model-token-step-oracle.json"
              "tinystories-1m-implementation-profile.json"
              "tinystories-1m-layernorm-semantics.json"
              "tinystories-1m-qdq-semantics.json"
            ]);
        };
        exactPackage = "${kev-gpt-src}/model_packages/tinystories-1m";
        modelId = "roneneldan/TinyStories-1M";
        revision = "ac533fb8b4f69c71894bf96badfe11e6294d9fcf";
        fetchModelFile = file: hash: pkgs.fetchurl {
          url = "https://huggingface.co/${modelId}/resolve/${revision}/${file}";
          inherit hash;
        };
        snapshot = pkgs.linkFarm "tinystories-1m-hf-snapshot" [
          { name = "config.json"; path = fetchModelFile "config.json"
              "sha256-/3TDDV67WrHaDy6kea33GXxQS0K1UiqFjDNKuR7UlYw="; }
          { name = "pytorch_model.bin"; path = fetchModelFile "pytorch_model.bin"
              "sha256-B/lgnqiCuBY/87I9QOK4LLcV1AljG+sVyEsWTzh32uc="; }
        ];
        closureReference = pkgs.runCommand "tasks-6-4-5-closure-cpu-reference" {
          nativeBuildInputs = [ pythonWithTinyStoriesTorchAO ];
        } ''
          export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
          export PYTHONDONTWRITEBYTECODE=1
          python3 ${compilerSource}/scripts/comparison/verify_closure_reference.py \
            --package ${exactPackage} --snapshot ${snapshot} \
            --reference-source ${kev-gpt-src} \
            --fixture ${./artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json} \
            --output "$out/reference.json"
        '';
        postpass = import ./nix/task6-bram-postpass.nix {
          inherit pkgs calyx;
          python = pythonWithTinyStoriesTorchAO;
          source = compilerSource;
        };
        image = import ./nix/task6-bram-image.nix {
          inherit pkgs postpass snapshot closureReference;
          python = pythonWithTinyStoriesTorchAO;
          package = exactPackage;
          source = compilerSource;
        };
        synthesis = import ./nix/task6-bram-synthesis.nix {
          inherit pkgs yosys image;
          python = pythonWithTinyStoriesTorchAO;
          source = compilerSource;
          inherit slang;
        };
        oneRomImage = import ./nix/task6-bram-one-rom-image.nix {
          inherit pkgs postpass image;
          python = pythonWithTinyStoriesTorchAO;
          source = compilerSource;
        };
        oneRomSynthesis = import ./nix/task6-bram-one-rom-synthesis.nix {
          inherit pkgs yosys slang;
          python = pythonWithTinyStoriesTorchAO;
          image = oneRomImage;
          source = compilerSource;
        };
        oneRomComparison = import ./nix/task6-bram-one-rom-comparison.nix {
          inherit pkgs;
          python = pkgs.python3;
          baseline = oneRomSynthesis;
          candidate = synthesis;
        };
        xc7 = openXC7.packages.${system};
        nextpnrSource = pkgs.fetchgit {
          name = "source";
          url = "https://github.com/RCoeurjoly/nextpnr-xilinx";
          rev = "32e8b798520d90e1130afe95908f04cf52125cf6";
          hash = "sha256-qKIxVFH1okPM1w2WFR0xgDisjRSw65qHGQpQa5eakgA=";
          fetchSubmodules = true;
        };
        chipdbNextpnr = xc7.nextpnr-xilinx.overrideAttrs (_: {
          src = nextpnrSource;
          patches = [
            ./patches/nextpnr-xilinx/0023-disconnect-lut6-a6-before-memory-constant.patch
            ./patches/nextpnr-xilinx/0024-drop-constant-a6-on-lut5.patch
            ./patches/nextpnr-xilinx/0037-router2-reserve-wire-index-storage.patch
          ];
        });
        chipdb = "${xc7.nextpnr-xilinx-chipdb.kintex7.override {
          chipdbFootprints = [ "xc7k480tffg1156" ];
          "nextpnr-xilinx" = chipdbNextpnr;
        }}/xc7k480tffg1156.bin";
        routeControl = chipdbNextpnr.overrideAttrs (_: {
          src = nextpnrSource;
          patches = [ ];
          postPatch = ''
            substituteInPlace common/placer1.cc \
              --replace-fail 'timing_analysis(ctx);' \
              'timing_analysis(ctx, true, true, true, false);'
          '';
        });
        geometryDiagnostic = routeControl.overrideAttrs (old: {
          pname = "nextpnr-xilinx-kintex-geometry-diagnostic";
          patches = (old.patches or [ ]) ++ [
            ./patches/nextpnr-xilinx/0038-allow-kintex-heap-geometry-diagnostic-overrides.patch
          ];
        });
        checkpointDiagnostic = geometryDiagnostic.overrideAttrs (old: {
          pname = "nextpnr-xilinx-kintex-checkpoint-diagnostic";
          patches = (old.patches or [ ]) ++ [
            ./patches/nextpnr-xilinx/0040-import-emitted-wire-to-wire-pip-names.patch
            ./patches/nextpnr-xilinx/0041-preserve-unique-pip-identity-in-checkpoints.patch
            ./patches/nextpnr-xilinx/0042-normalize-evidenced-legacy-slice-clock-selectors.patch
            ./patches/nextpnr-xilinx/0043-recover-canonical-int-pip-aliases-from-original-fasm.patch
            ./patches/nextpnr-xilinx/0044-ignore-functionally-unused-postroute-lut-input-arcs.patch
          ];
        });
        nextpnr = checkpointDiagnostic.overrideAttrs (old: {
          pname = "nextpnr-xilinx-kintex-bram-mode-fix";
          patches = (old.patches or [ ]) ++ [
            ./patches/nextpnr-xilinx/0045-emit-ramb36-odd-width-mode-features.patch
          ];
        });
        constraints = ./fpga/constraints/tinystories_1m_ypcb_bram_production.xdc;
        placement = import ./nix/task6-bram-placement.nix {
          inherit pkgs synthesis nextpnr chipdb constraints;
          python = pkgs.python3;
          source = compilerSource;
        };
        route = import ./nix/task6-bram-route.nix {
          inherit pkgs placement nextpnr chipdb constraints;
          python = pkgs.python3;
          source = compilerSource;
        };
        bitstream = import ./nix/task6-bram-bitstream.nix {
          inherit pkgs nextpnr route;
          bitstreamPkgs = import inputs.bitstream-nixpkgs { inherit system; };
          openXC7Packages = xc7;
        };
        boardEvidence = import ./nix/task4-board-evidence.nix {
          inherit pkgs bitstream;
          python = pkgs.python312;
        };
        family = import ./nix/tinystories-family.nix {
          inherit pkgs;
          tinyStories1m = { inherit revision snapshot; };
        };
        familySizes = [ "1M" "3M" "8M" "28M" "33M" ];
        familySource = builtins.path {
          path = ./.;
          name = "task5-family-compiler-source";
          filter = path: type:
            let rel = pkgs.lib.removePrefix "${toString ./.}/" (toString path);
            in path == toString ./. || builtins.elem rel [
              "TinyStories" "scripts" "scripts/pipeline" "scripts/comparison" "artifacts"
              "artifacts/reference"
            ] || (pkgs.lib.hasPrefix "TinyStories/" rel && pkgs.lib.hasSuffix ".py" rel)
              || (pkgs.lib.hasPrefix "scripts/pipeline/" rel && pkgs.lib.hasSuffix ".py" rel)
              || rel == "scripts/comparison/verify_closure_reference.py"
              || (pkgs.lib.hasPrefix "artifacts/reference/" rel
                  && pkgs.lib.hasSuffix ".json" rel
                  && rel != "artifacts/reference/tinystories-1m-bram-production-nextpnr.json");
        };
        familyPackages = import ./nix/tinystories-fixed-point-family.nix {
          inherit pkgs family;
          python = pythonWithTinyStoriesTorchAO;
          source = familySource;
          packageLibrary = kev-gpt-src;
          acceptedPackage = exactPackage;
        };
        familyManifest = pkgs.writeText "task5-tinystories-family-inputs.json"
          (builtins.toJSON (map (size:
            let model = family.${size};
            in { size_label = size; model_id = model.modelId;
                 inherit (model) revision;
                 config_sha256 = model.configSha256;
                 weights_sha256 = model.weightsSha256;
                 snapshot = "${model.snapshot}"; }) familySizes));
        familyInventory = pkgs.runCommand "task5-tinystories-family-inventory" {
          nativeBuildInputs = [ pythonWithTinyStoriesTorchAO ];
        } ''
          mkdir -p "$out"
          python ${./scripts/analysis/tinystories_family_inventory.py} \
            --manifest ${familyManifest} --output "$out/inventory.json"
        '';
        familyOracles = builtins.mapAttrs (size: model:
          pkgs.runCommand "task5-tinystories-${size}-fixed-point-oracle" {
            nativeBuildInputs = [ pythonWithTinyStoriesTorchAO pkgs.coreutils ];
          } ''
            export PYTHONPATH=${familySource}
            export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
            mkdir -p "$out"
            timeout 900 python ${familySource}/scripts/pipeline/capture_tinystories_family_oracle.py \
              --snapshot ${model.snapshot} --package ${familyPackages.${size}} \
              --revision ${model.revision} --size ${size} \
              --steps ${if size == "1M" then "16" else "1"} \
              ${if size == "1M" then "--expected-generation ${./artifacts/reference/tinystories-1m-exact-generation.json} --expected-logits ${./artifacts/reference/tinystories-1m-fixed-logits-oracle.json}" else ""} \
              --output "$out/oracle.json"
          '') family;
        familyCompiler = import ./nix/tinystories-family-compiler.nix {
          inherit pkgs calyx family;
          python = pythonWithTinyStoriesTorchAO;
          packages = familyPackages;
          oracles = familyOracles;
          source = familySource;
        };
        familyImages = import ./nix/tinystories-family-image.nix {
          inherit pkgs family;
          python = pythonWithTinyStoriesTorchAO;
          packages = familyPackages;
          oracles = familyOracles;
          compiler = familyCompiler;
          source = familySource;
        };
        familySimulations = import ./nix/tinystories-family-first-token-sim.nix {
          inherit pkgs family;
          python = pythonWithTinyStoriesTorchAO;
          oracles = familyOracles;
          images = familyImages;
          source = familySource;
        };
        familySyntheses = import ./nix/tinystories-family-synthesis.nix {
          inherit pkgs yosys;
          python = pkgs.python3;
          slang = familySlang;
          images = familyImages;
          simulations = familySimulations;
        };
        oneM16Image = pkgs.runCommand "task5-tinystories-1M-16token-image" {
          nativeBuildInputs = [ pythonWithTinyStoriesTorchAO pkgs.coreutils ];
        } ''
          export PYTHONPATH=${familySource}
          export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
          mkdir -p "$out"
          timeout 2400 python ${./scripts/analysis/build_tinystories_family_16token_image.py} \
            --snapshot ${family."1M".snapshot} --package ${familyPackages."1M"} \
            --revision ${family."1M".revision} \
            --oracle ${familyOracles."1M"}/oracle.json \
            --reference-image ${familyImages."1M"} \
            --calyx ${calyx}/bin/calyx --calyx-library ${calyx}/share/calyx \
            --output "$out"
          test -s "$out/model-static-init-normalized.sv"
          test -s "$out/image.json"
        '';
        oneM16Simulation = pkgs.runCommand "task5-tinystories-1M-16token-simulation" {
          nativeBuildInputs = [ pythonWithTinyStoriesTorchAO pkgs.verilator pkgs.gnumake pkgs.gcc pkgs.coreutils ];
        } ''
          export PYTHONPATH=${familySource}
          export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
          mkdir -p "$out"
          timeout 43200 python ${./scripts/analysis/simulate_tinystories_family_16token.py} \
            --image ${oneM16Image} --oracle ${familyOracles."1M"}/oracle.json \
            --output "$out"
          test -s "$out/result.json"
          test -s "$out/execution.jsonl"
        '';
        familyStageInputs = pkgs.writeText "task5-family-stage-inputs.json"
          (builtins.toJSON (builtins.listToAttrs (map (size: {
            name = size;
            value = {
              compiler = "${familyCompiler.${size}}";
              compiler_drv = builtins.unsafeDiscardStringContext familyCompiler.${size}.drvPath;
              image = "${familyImages.${size}}";
              image_drv = builtins.unsafeDiscardStringContext familyImages.${size}.drvPath;
              simulation = "${familySimulations.${size}}";
              simulation_drv = builtins.unsafeDiscardStringContext familySimulations.${size}.drvPath;
            } // (if builtins.elem size [ "28M" "33M" ] then {} else {
              synthesis = "${familySyntheses.${size}}";
              synthesis_drv = builtins.unsafeDiscardStringContext familySyntheses.${size}.drvPath;
            });
          }) familySizes)));
        familyStageMatrix = pkgs.runCommand "task5-tinystories-family-stage-matrix" {
          nativeBuildInputs = [ pkgs.python3 ];
        } ''
          mkdir -p "$out"
          python ${./scripts/analysis/report_tinystories_family_stages.py} \
            --inventory ${familyInventory}/inventory.json \
            --inputs ${familyStageInputs} \
            --archived-synthesis ${./artifacts/task5/archived-synthesis.json} \
            --board-capacity ${./artifacts/reference/tinystories-1m-bram-production-nextpnr.json} \
            --output "$out/matrix.json" --markdown "$out/report.md"
        '';
      in {
        checks.closure-reference = closureReference;
        checks.graph-kernel-rtl = graphKernelRtl;
        checks.graph-memory-reductions-rtl = graphMemoryReductions;
        checks.graph-stable-scratch-rtl = graphStableScratchRtl;
        checks.graph-lut-memory-rtl = graphLutMemoryRtl;
        checks.graph-lut-memory-fullwidth-rtl = graphLutMemoryFullWidthRtl;
        checks.graph-runtime-verifier = graphRuntimeVerifier;
        checks.graph-integer-boundary = graphIntegerBoundary;
        checks.graph-producer-slices = graphProducerSlices;
        checks.graph-pointwise-fusion = graphPointwiseFusion;
        checks.graph-generation-link = graphGenerationLink;
        checks.graph-core-source = graphCoreSourceCheck;
        checks.graph-core-inputs = graphCoreInputCheck;
        checks.graph-horizontal-fusion = graphHorizontalFusion;
        checks.graph-one-trip-loops = graphOneTripLoops;
        checks.graph-narrow-rom = graphNarrowRom;
        devShells.default = pkgs.mkShell {
          packages = [ pkgs.python312 ];
        };
        # Legacy reproduction is available only under explicit historical names.
        packages = {
          graph-yosys = graphYosys;
          graph-calyx-backend = graphCalyxBackend;
          graph-four-state-verifier = graphFourStateVerifier;
          graph-torch-opt = graphToolchain.driver;
          graph-calyx-opt = graphCalyxDriver;
          graph-circt = graphCirct;
          graph-kernel-rtl-check = graphKernelRtl;
          graph-exact-reductions-rtl-check = graphExactReductions;
          graph-memory-reductions-rtl-check = graphMemoryReductions;
          graph-stable-scratch-rtl-check = graphStableScratchRtl;
          graph-lut-memory-rtl-check = graphLutMemoryRtl;
          graph-lut-memory-fullwidth-rtl-check = graphLutMemoryFullWidthRtl;
          graph-runtime-verifier-check = graphRuntimeVerifier;
          graph-integer-boundary-check = graphIntegerBoundary;
          graph-producer-slices-check = graphProducerSlices;
          graph-pointwise-fusion-check = graphPointwiseFusion;
          graph-generation-link-check = graphGenerationLink;
          graph-litedram-python = graphLiteDram.python;
          graph-litedram-export-check = graphLiteDramExportCheck;
          graph-ypcb-ch0-controller = graphBoardController;
          graph-core-source-check = graphCoreSourceCheck;
          graph-core-input-check = graphCoreInputCheck;
          graph-torch-driver = graphToolchain.driver;
          graph-core-1m = graphCore1m;
          graph-core-1m-memory = graphCoreMemory1m;
          graph-core-1m-memory-synthesis-export = graphCoreMemorySynthesis;
          graph-core-1m-stable-scratch = graphCoreStableScratch1m;
          graph-core-1m-stable-scratch-synthesis-export = graphCoreStableScratchSynthesis;
          graph-core-1m-exact = graphCoreExact1m;
          graph-core-1m-exact-synthesis-export = graphCoreExactSynthesis;
          graph-core-1m-ddr = graphCoreDdr1m;
          graph-core-1m-synthesis-export = graphCoreSynthesis;
          graph-core-1m-ddr-synthesis-export = graphCoreDdrSynthesis;
          graph-core-1m-ddr-xc7-coarse = graphCoreDdrCoarse;
          graph-core-1m-ddr-xc7-ram = graphCoreDdrRam;
          graph-core-1m-ddr-xc7-final = graphCoreDdrFinal;
          graph-core-1m-ddr-transport = graphCoreDdrTransport;
          # Common compilation stages; neither target grants board acceptance.
          task4-graph-core = graphCoreDdr1m;
          task6-graph-core = graphCoreDdr1m;
          task6-graph-xc7-coarse = graphCoreDdrCoarse;
          task6-graph-xc7-ram = graphCoreDdrRam;
          task6-graph-xc7-final = graphCoreDdrFinal;
          task4-graph-ddr-transport = graphCoreDdrTransport;
          task6-graph-ddr-transport = graphCoreDdrTransport;
          graph-horizontal-fusion-check = graphHorizontalFusion;
          graph-one-trip-loops-check = graphOneTripLoops;
          graph-narrow-rom-check = graphNarrowRom;
          graph-torch-mlir = graphToolchain.torchMlir;
          graph-torch-mlir-sdk = graphToolchain.mlir;
          closure-cpu-reference = closureReference;
          historical-task6-bram-postpass = postpass;
          historical-task6-bram-image = image;
          historical-task6-bram-synthesis = synthesis;
          historical-task6-bram-one-rom-comparison = oneRomComparison;
          historical-task6-bram-placement = placement;
          historical-task6-bram-route = route;
          historical-task6-bram-bitstream = bitstream;
          historical-task4-board-evidence = boardEvidence;
          historical-task5-tinystories-family-inventory = familyInventory;
          historical-task5-tinystories-family-stage-matrix = familyStageMatrix;
          historical-task5-tinystories-1M-16token-image = oneM16Image;
          historical-task5-tinystories-1M-16token-simulation = oneM16Simulation;
        } // builtins.listToAttrs (map (size: {
          name = "historical-task5-tinystories-${size}-package";
          value = familyPackages.${size};
        }) familySizes)
          // builtins.listToAttrs (map (size: {
            name = "historical-task5-tinystories-${size}-generated-sv";
            value = familyCompiler.${size};
          }) familySizes)
          // builtins.listToAttrs (map (size: {
            name = "historical-task5-tinystories-${size}-image";
            value = familyImages.${size};
          }) familySizes)
          // builtins.listToAttrs (map (size: {
            name = "historical-task5-tinystories-${size}-first-token-simulation";
            value = familySimulations.${size};
          }) familySizes)
          // builtins.listToAttrs (map (size: {
            name = "historical-task5-tinystories-${size}-synthesis";
            value = familySyntheses.${size};
          }) familySizes);
      });
}
