# Torch's January LLVM SDK is a separate ABI from CIRCT's March SDK.
{ pkgs, python }:
let
  llvmPackages = (pkgs.llvmPackages_git.override {
    llvmVersions."22.0.0-git".gitRelease = {
      rev = "3ca2a5fc0b84762f0e7d8a0e613fd69f7e344219";
      rev-version = "23.0.0-unstable-2026-01-20";
      sha256 = "sha256-jjdb2PtKnjYo9RIGJ82YtKmZinqEOlmm7R64SeJqTac=";
    };
  }).overrideScope (final: prev: {
    libllvm = (prev.libllvm.override { buildLlvmPackages = final; }).overrideAttrs (old: {
      postConfigure = "";
      doCheck = false;
      cmakeFlags = (old.cmakeFlags or []) ++ [
        (pkgs.lib.cmakeBool "LLVM_BUILD_TESTS" false)
        (pkgs.lib.cmakeBool "LLVM_INCLUDE_TESTS" false)
      ];
    });
  });
  nanobind = pkgs.callPackage ./graph-nanobind.nix { inherit python; };
  mlir = (llvmPackages.mlir.override {
    devExtraCmakeFlags = [ (pkgs.lib.cmakeBool "MLIR_ENABLE_BINDINGS_PYTHON" true) ];
  }).overrideAttrs (old: {
    doCheck = false;
    nativeBuildInputs = old.nativeBuildInputs ++ [ python ];
    preConfigure = (old.preConfigure or "") + ''
      export PYTHONPATH="${python.pkgs.pybind11}/${python.sitePackages}:${nanobind}/${python.sitePackages}:''${PYTHONPATH:-}"
    '';
    cmakeFlags = (old.cmakeFlags or []) ++ [
      (pkgs.lib.cmakeBool "LLVM_BUILD_TESTS" false)
      (pkgs.lib.cmakeFeature "Python3_EXECUTABLE" "${python}/bin/python3")
      (pkgs.lib.cmakeFeature "Python_EXECUTABLE" "${python}/bin/python3")
      (pkgs.lib.cmakeFeature "pybind11_DIR" "${python.pkgs.pybind11}/${python.sitePackages}/pybind11/share/cmake/pybind11")
      (pkgs.lib.cmakeFeature "nanobind_DIR" "${nanobind}/${python.sitePackages}/nanobind/cmake")
    ];
  });
  torchMlir = pkgs.callPackage ./graph-torch-mlir.nix {
    inherit python nanobind mlir;
    inherit (llvmPackages) llvm tblgen;
  };
in {
  inherit mlir torchMlir;
  inherit (llvmPackages) llvm;
  driver = pkgs.callPackage ./graph-compiler-passes.nix {
    inherit mlir torchMlir;
    inherit (llvmPackages) llvm;
  };
}
