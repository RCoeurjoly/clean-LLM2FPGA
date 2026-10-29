# The caller supplies the same pinned LLVM/MLIR pair used to build Torch-MLIR.
# A matching major version alone is insufficient: CIRCT uses a different SDK revision.
{ lib, stdenv, cmake, python3, llvm, mlir, torchMlir }:
let
  installTests = lib.fileset.toSource {
    root = ../tests;
    fileset = lib.fileset.unions [
      ../tests/test_graph_input_origins.py
      ../tests/test_graph_pointwise_pingpong.py
      ../tests/test_graph_capacity_pool.py
      ../tests/test_graph_gemv_epilogue.py
      ../tests/test_graph_restoring_sqrt.py
      ../tests/test_graph_sequential_scratch.py
      ../tests/test_graph_function_sharing.py
      ../tests/test_graph_loop_outlining.py
      ../tests/test_graph_private_storage.py
      ../tests/test_graph_index_scale_normalization.py
    ];
  };
  testSupport = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [ ../compiler/pipeline.py ../compiler/stages.py ];
  };
in stdenv.mkDerivation {
  pname = "llm2fpga-typed-torch-driver";
  version = "0.1.0";
  src = ../compiler/passes;
  nativeBuildInputs = [ cmake python3 ];
  buildInputs = [ llvm mlir torchMlir ];
  cmakeFlags = [
    (lib.cmakeFeature "LLVM_DIR" "${llvm.dev or llvm}/lib/cmake/llvm")
    (lib.cmakeFeature "MLIR_DIR" "${mlir.dev or mlir}/lib/cmake/mlir")
    (lib.cmakeFeature "TORCH_MLIR_ROOT" (toString torchMlir))
  ];
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPATH="${testSupport}"
    export LLM2FPGA_TORCH_PASS_TOOL="$out/bin/llm2fpga-torch-opt"
    export LLM2FPGA_MLIR_RUNNER="${mlir}/bin/mlir-runner"
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_input_origins.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_sequential_scratch.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_function_sharing.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_loop_outlining.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_private_storage.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_index_scale_normalization.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_restoring_sqrt.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_pointwise_pingpong.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_gemv_epilogue.py -v
    ${python3}/bin/python3 -m unittest discover \
      -s ${installTests} -p test_graph_capacity_pool.py -v
    runHook postInstallCheck
  '';
}
