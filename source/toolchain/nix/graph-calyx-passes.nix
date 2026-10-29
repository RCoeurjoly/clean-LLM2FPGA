# The Calyx driver must use CIRCT's SDK, independently of the Torch SDK.
{ lib, runCommand, stdenv, cmake, ninja, python3, zlib, circt, circtSource, calyx, baselineCalyx, fourStateVerifier, mlir, libllvm }:
let
  installTests = lib.fileset.toSource {
    root = ../tests;
    fileset = lib.fileset.fileFilter
      (file: file.hasExt "py" &&
        (lib.hasPrefix "test_graph_memory" file.name ||
         lib.hasPrefix "test_graph_calyx" file.name)) ../tests;
  };
  testSupport = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../scripts/pipeline/memory_policy.py
      ../scripts/comparison/verify_graph_offchip_rtl.py
      ../compiler/ddr_images.py
      ../compiler/memory_plan.py
      ../compiler/pipeline.py
      ../compiler/stages.py
      ../compiler/tests/calyx/combinational-sharing-cycle.futil
      ../compiler/tests/calyx/signed-divider-sharing.futil
      ../compiler/tests/calyx/signed-divider-parallel.futil
      ../rtl/primitives/closure_ddr_read_port.sv
      ../rtl/primitives/closure_ddr_read_bank.sv
      ../rtl/primitives/closure_ddr_read_cdc.sv
      ../rtl/primitives/closure_ddr_image_loader.sv
      ../rtl/primitives/closure_ddr_loaded_port.sv
      ../rtl/primitives/closure_memory.sv
      ../rtl/primitives/closure_narrow_rom.sv
      ../calyx/primitives/closure_memory.futil
    ];
  };
  package = stdenv.mkDerivation {
    pname = "llm2fpga-typed-calyx-driver";
    version = "0.1.0";
    src = lib.cleanSource ../compiler/passes/calyx;
    nativeBuildInputs = [ cmake ninja ];
    buildInputs = [ circt mlir libllvm zlib ];
    postPatch = ''
      cp ${circtSource}/lib/Transforms/FlattenMemRefs.cpp FlattenMemRefs.cpp
      chmod u+w FlattenMemRefs.cpp
      patch -p1 < ${./patches/graph-memref-row-major-flatten.patch}
      cp ${circtSource}/lib/Dialect/Calyx/Export/CalyxEmitter.cpp CalyxEmitter.cpp
      chmod u+w CalyxEmitter.cpp
      patch -p1 < ${./patches/graph-calyx-reference-primitive-export.patch}
      patch -p1 < ${./patches/graph-calyx-group-extern-files.patch}
      patch -p1 < ${./patches/graph-calyx-port-interval-export.patch}
      patch -p1 < ${./patches/graph-calyx-typed-invoke-bindings.patch}
      cp ${circtSource}/lib/Conversion/SCFToCalyx/SCFToCalyx.cpp SCFToCalyx.cpp
      chmod u+w SCFToCalyx.cpp
      patch -p1 < ${./patches/graph-calyx-input-origin.patch}
      patch -p1 < ${./patches/graph-calyx-typed-block-names.patch}
      patch -p1 < ${./patches/graph-calyx-cached-comb-inlining.patch}
      patch -p1 < ${./patches/graph-calyx-control-flow-dependency.patch}
      patch -p1 < ${./patches/graph-calyx-cached-group-elimination.patch}
      patch -p1 < ${./patches/graph-calyx-indexed-memory-ports.patch}
      patch -p1 < ${./patches/graph-calyx-unsigned-index-casts.patch}
    '';
    cmakeFlags = [
      "-GNinja"
      (lib.cmakeFeature "MLIR_DIR" "${mlir.dev}/lib/cmake/mlir")
      (lib.cmakeFeature "LLVM_DIR" "${libllvm.dev}/lib/cmake/llvm")
      (lib.cmakeFeature "CIRCT_DIR" "${circt.dev}/lib/cmake/circt")
    ];
    passthru.tests = runCommand "llm2fpga-calyx-driver-tests" {
      nativeBuildInputs = [ python3 ];
    } ''
      export PYTHONDONTWRITEBYTECODE=1
      export PYTHONPATH="${testSupport}"
      export LLM2FPGA_TEST_SUPPORT="${testSupport}"
      export PATH="${fourStateVerifier}/bin:$PATH"
      export LLM2FPGA_CALYX_PASS_TOOL="${package}/bin/llm2fpga-calyx-opt"
      export LLM2FPGA_MEMORY_TOOL="${package}/bin/llm2fpga-calyx-opt"
      export LLM2FPGA_FLATTEN_MLIR_TOOL="${mlir}/bin/mlir-opt"
      export LLM2FPGA_FLATTEN_MLIR_RUNNER="${mlir}/bin/mlir-runner"
      export LLM2FPGA_CALYX_TRANSLATE_TOOL="${package}/bin/llm2fpga-calyx-translate"
      export LLM2FPGA_CALYX_TOOL="${calyx}/bin/calyx"
      ${python3}/bin/python3 -m unittest discover \
        -s ${installTests} -p 'test_graph_memory*.py' -v
      export LLM2FPGA_CALYX_TRANSLATE_TOOL="${package}/bin/llm2fpga-calyx-translate"
      export LLM2FPGA_CALYX_TOOL="${calyx}/bin/calyx"
      ${python3}/bin/python3 -m unittest discover \
        -s ${installTests} -p 'test_graph_calyx*.py' -v
      ${python3}/bin/python3 ${../scripts/comparison/verify_graph_guard_disjointness.py} \
        --before ${baselineCalyx}/bin/calyx --after ${calyx}/bin/calyx \
        --iverilog ${fourStateVerifier}/bin/iverilog --vvp ${fourStateVerifier}/bin/vvp \
        --output "$TMPDIR/guard-four-state.json"
      touch "$out"
    '';
  };
in package
