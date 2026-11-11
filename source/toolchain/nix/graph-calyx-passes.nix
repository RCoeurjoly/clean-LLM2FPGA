{ lib, stdenv, cmake, ninja, zlib, circt, circtSource, mlir, libllvm }:
stdenv.mkDerivation {
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
      patch -p1 < ${./patches/calyx-exact-float-constants.patch}
      cp ${circtSource}/lib/Conversion/SCFToCalyx/SCFToCalyx.cpp SCFToCalyx.cpp
      chmod u+w SCFToCalyx.cpp
      patch -p1 < ${./patches/graph-calyx-input-origin.patch}
      patch -p1 < ${./patches/graph-calyx-typed-block-names.patch}
      patch -p1 < ${./patches/graph-calyx-cached-comb-inlining.patch}
      patch -p1 < ${./patches/graph-calyx-control-flow-dependency.patch}
      patch -p1 < ${./patches/graph-calyx-cached-group-elimination.patch}
      patch -p1 < ${./patches/graph-calyx-indexed-memory-ports.patch}
      patch -p1 < ${./patches/graph-calyx-unsigned-index-casts.patch}
      patch -p1 < ${./patches/calyx-read-memory-ports.patch}
      patch -p1 < ${./patches/calyx-float-cast-handshake.patch}
      patch -p1 < ${./patches/calyx-float-format-conversion.patch}
    '';
    cmakeFlags = [
      "-GNinja"
      (lib.cmakeFeature "MLIR_DIR" "${mlir.dev}/lib/cmake/mlir")
      (lib.cmakeFeature "LLVM_DIR" "${libllvm.dev}/lib/cmake/llvm")
      (lib.cmakeFeature "CIRCT_DIR" "${circt.dev}/lib/cmake/circt")
    ];
}
