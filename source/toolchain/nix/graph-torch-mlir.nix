{ lib, stdenv, fetchFromGitHub, cmake, ninja, python, nanobind, tblgen, mlir, llvm
, zlib, libxml2, ncurses }:
stdenv.mkDerivation {
  pname = "llm2fpga-graph-torch-mlir";
  version = "0-unstable-2026-02-12";
  src = fetchFromGitHub {
    owner = "llvm";
    repo = "torch-mlir";
    rev = "59c249e5cc2025acca81bdcf1596b8dd36a5c0f9";
    # LLVM/MLIR come from the separately pinned SDK; StableHLO is disabled.
    hash = "sha256-vbrwuse2NI1AVORjsN8Lqo4SZevyeOomseVyZDLPSQY=";
  };
  patches = [ ./patches/graph-torch-shared-mlir.patch ./patches/graph-torch-tablegen-path.patch ];
  nativeBuildInputs = [ cmake ninja python python.pkgs.pybind11 nanobind tblgen ];
  buildInputs = [ zlib libxml2 ncurses mlir llvm ];
  preConfigure = ''
    export PYTHONPATH="${python.pkgs.pybind11}/${python.sitePackages}:${nanobind}/${python.sitePackages}:''${PYTHONPATH:-}"
  '';
  cmakeFlags = [
    # Makefiles places the Python module's -z,undefs before LLVM's -z,defs.
    # Ninja preserves the intended order for interpreter-resolved symbols.
    "-GNinja"
    (lib.cmakeBool "LLVM_ENABLE_ASSERTIONS" true)
    (lib.cmakeBool "MLIR_LINK_MLIR_DYLIB" true)
    (lib.cmakeBool "MLIR_ENABLE_BINDINGS_PYTHON" true)
    (lib.cmakeBool "TORCH_MLIR_ENABLE_STABLEHLO" false)
    (lib.cmakeFeature "LLM2FPGA_MLIR_TABLEGEN" "${tblgen}/bin/mlir-tblgen")
    (lib.cmakeFeature "LLVM_DIR" "${llvm.dev or llvm}/lib/cmake/llvm")
    (lib.cmakeFeature "MLIR_DIR" "${mlir.dev or mlir}/lib/cmake/mlir")
    (lib.cmakeFeature "nanobind_DIR" "${nanobind}/${python.sitePackages}/nanobind/cmake")
    (lib.cmakeFeature "Python3_EXECUTABLE" "${python}/bin/python3")
    (lib.cmakeFeature "Python_EXECUTABLE" "${python}/bin/python3")
  ];
  buildPhase = ''
    runHook preBuild
    cmake --build . --target torch-mlir-opt TorchMLIRPythonModules -- -j''${NIX_BUILD_CORES:-1}
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    for component in torch-mlir-headers torch-mlir-opt TorchMLIRPythonModules; do
      cmake --install . --prefix "$out" --component "$component"
    done
    mkdir -p "$out/${python.sitePackages}"
    cp -r "$out/python_packages/." "$out/${python.sitePackages}/"
    runHook postInstall
  '';
}
