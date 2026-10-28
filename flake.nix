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
        pipeline = import ./nix/bitstream.nix { inherit pkgs tools; };
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
          default = pipeline.bitstream;
        };
        checks.build-driver = driverTests;
        devShells.default = pkgs.mkShell {
          packages = [ pkgs.python312 pkgs.nix ];
        };
      });
}
