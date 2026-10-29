{
  description = "Selected TinyStories-1M PyTorch-to-bitstream build";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/6fd329b2adfecb86ae49c1cba89689bd0f229e04";
    nixpkgs-llvm21.url = "github:NixOS/nixpkgs/346dd96ad74dc4457a9db9de4f4f57dab2e5731d";
    bitstream-nixpkgs.url = "github:NixOS/nixpkgs/6fd329b2adfecb86ae49c1cba89689bd0f229e04";
    flake-utils.url = "github:numtide/flake-utils/11707dc2f618dd54ca8739b309ec4fc024de578b";
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
    openXC7.url = "github:RCoeurjoly/toolchain-nix/aebe5a3cf24ddd84818fb9a96daa96b8b76c796b";
    calyx-src = {
      url = "github:calyxir/calyx/5a4303847392609cad83dda6f4bdffc8cc0e5c89";
      flake = false;
    };
    kev-gpt-src = {
      url = "github:RCoeurjoly/kev-gpt/df1fc45b2ffcb26fddc19cfd57621e7eedf6153f";
      flake = false;
    };
  };

  outputs = inputs@{ nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        tools = import ./nix/selected-toolchain.nix { inherit inputs system; };
        pipeline = import ./nix/pipeline.nix { inherit pkgs tools; };
        bitstream = import ./nix/bitstream.nix { inherit pkgs tools; inherit (pipeline) fasm; };
        driverSource = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./build.py ./tools/bit_header.py ./tools/verify_decoded_frames.py
            ./tools/test_full_build.py ./tools/test_bit_header.py
          ];
        };
        driverTests = pkgs.runCommand "bitstream-build-driver-tests" {
          nativeBuildInputs = [ pkgs.python312 ];
          PYTHONDONTWRITEBYTECODE = "1";
        } ''
          cd ${driverSource}
          python3 -m unittest tools.test_full_build tools.test_bit_header
          touch "$out"
        '';
      in {
        packages = tools // pipeline // {
          inherit bitstream;
          default = bitstream;
          route = pipeline.fasm;
        };
        checks.build-driver = driverTests;
        checks.graph-torch-opt = tools.graph-torch-opt.tests;
        checks.graph-calyx-opt = tools.graph-calyx-opt.tests;
        checks.graph-calyx-backend = tools.graph-calyx-backend.tests;
        checks.graph-yosys = tools.graph-yosys.tests;
        checks.graph-four-state-verifier = tools.graph-four-state-verifier.tests;

        checks.pytorch-equivalence = pkgs.runCommand "pytorch-equivalence-tests" {
          nativeBuildInputs = tools.graph-core-1m-stable-scratch.nativeBuildInputs;
          PYTHONPATH = tools.graph-core-1m-stable-scratch.PYTHONPATH;
          PYTHONDONTWRITEBYTECODE = "1";
          LLM2FPGA_TORCH_PASS_TOOL = "${tools.graph-torch-opt}/bin/llm2fpga-torch-opt";
          LLM2FPGA_MLIR_RUNNER = "${tools.graph-torch-mlir-sdk}/bin/mlir-runner";
        } ''
          python3 - <<'PYTHON'
from pathlib import Path
import os
from compiler.build_profile import CoreBuildProfile
from compiler.pipeline import CompilerTool
CoreBuildProfile(
    Path("${tools.graph-core-1m-stable-scratch.compilerSource}/compiler/contracts/tinystories-1m-fixed-point.json"),
    {"torch": CompilerTool(Path(os.environ["LLM2FPGA_TORCH_PASS_TOOL"]))},
).validate()
PYTHON
          mkdir tests
          cp ${./source/toolchain/tests/test_graph_pointwise_fusion.py} tests/test_graph_pointwise_fusion.py
          python3 -m unittest discover -s tests -v
          touch "$out"
        '';

        devShells.default = pkgs.mkShell {
          packages = [ pkgs.python312 pkgs.nix ];
        };
      });
}
