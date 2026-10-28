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

  outputs = inputs@{ self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        tools = import ./nix/selected-toolchain.nix { inherit inputs system; };
        flow = builtins.fromJSON (builtins.readFile ./pipeline/flow.json);
        nativeToolDirs = pkgs.lib.unique (map builtins.dirOf
          (builtins.filter (path: pkgs.lib.hasPrefix "/nix/store/" path
            && !(pkgs.lib.hasSuffix "/python3" path))
            (map (stage: builtins.head stage.argv) flow.stages)));
        bitstreamEnvironment = builtins.fromJSON (builtins.readFile ./pipeline/bitstream.json);
        # Run outside a Nix build sandbox: the recorded host compiler and
        # bubblewrap namespace are required by the exact-image flow.
        runner = name: command: pkgs.writeShellApplication {
          inherit name;
          runtimeInputs = [ pkgs.python312 pkgs.bubblewrap pkgs.nix pkgs.gzip ];
          text = command;
        };
        reproduce = runner "reproduce-bitstream" ''
          exec python3 ${self}/build.py all --output "$PWD/build/full" "$@"
        '';
        verify = runner "verify-bitstream-build" ''
          exec python3 ${self}/build.py verify "$@"
        '';
        importTools = runner "import-bitstream-toolchain" ''
          gzip -dc ${self}/toolchain/closure.nar.gz | nix-store --import
        '';
        driverSource = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./build.py ./tools ./pipeline/flow.json ./toolchain/paths.json
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
        packages = tools // {
          default = reproduce;
          reproduce-bitstream = reproduce;
        };
        apps = {
          default = { type = "app"; program = "${reproduce}/bin/reproduce-bitstream"; meta.description = "Reproduce the selected bitstream"; };
          build = { type = "app"; program = "${reproduce}/bin/reproduce-bitstream"; meta.description = "Reproduce the selected bitstream"; };
          verify = { type = "app"; program = "${verify}/bin/verify-bitstream-build"; meta.description = "Verify pinned build inputs"; };
          import-toolchain = { type = "app"; program = "${importTools}/bin/import-bitstream-toolchain"; meta.description = "Import the recorded toolchain archive"; };
        };
        checks.build-driver = driverTests;
        devShells.default = pkgs.mkShell {
          packages = [ pkgs.python312 pkgs.bubblewrap pkgs.nix pkgs.gzip ];
          shellHook = ''
            export PRJXRAY_DB_DIR=${pkgs.lib.escapeShellArg bitstreamEnvironment.PRJXRAY_DB_DIR}
            export PRJXRAY_PYTHON_DIR=${pkgs.lib.escapeShellArg bitstreamEnvironment.PRJXRAY_PYTHON_DIR}
            export PYTHONPATH=${pkgs.lib.escapeShellArg bitstreamEnvironment.PYTHONPATH}
            export LLM2FPGA_PYTORCH_PYTHON=${pkgs.lib.escapeShellArg (builtins.head flow.core.argv)}
            export PATH=${pkgs.lib.escapeShellArg (builtins.concatStringsSep ":" nativeToolDirs)}:${pkgs.lib.escapeShellArg bitstreamEnvironment.PATH}:"$PATH"
          '';
        };
      });
}
