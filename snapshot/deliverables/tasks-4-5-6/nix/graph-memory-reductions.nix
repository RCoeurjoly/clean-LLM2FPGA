# Complete toy-generation/public-memory and private-capacity RTL regression.
{ lib, runCommand, stdenv, gnumake, verilator, python, torchMlir, torchDriver, circt, memoryDriver, calyx
, stableScratchPooling ? false, lutMemoryMaxBits ? 0, fullWidthLutRoms ? false }:
assert builtins.isBool stableScratchPooling;
assert builtins.isInt lutMemoryMaxBits && lutMemoryMaxBits >= 0;
assert builtins.isBool fullWidthLutRoms;
assert !fullWidthLutRoms || lutMemoryMaxBits > 0;
let
  compilerSource = import ./graph-core-source.nix { inherit lib; };
  verification = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../scripts/comparison/verify_graph_memory_reductions_rtl.py
      ../scripts/comparison/graph_rtl_runtime.py
    ];
  };
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
in runCommand "graph-memory-reductions-rtl-check" {
  nativeBuildInputs = [ pythonEnv stdenv.cc gnumake verilator ];
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONPATH = "${compilerSource}:${verification}/scripts/comparison:${torchMlir}/${python.sitePackages}:${torchMlir}/${python.sitePackages}/torch_mlir";
} ''
  mkdir -p "$out"
  python ${verification}/scripts/comparison/verify_graph_memory_reductions_rtl.py \
    ${lib.optionalString stableScratchPooling "--stable-scratch-pooling"} \
    --lut-memory-max-bits ${toString lutMemoryMaxBits} \
    ${lib.optionalString fullWidthLutRoms "--full-width-lut-roms"} \
    --torch ${torchDriver}/bin/llm2fpga-torch-opt --circt ${circt}/bin/circt-opt \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator \
    --work-dir "$out/work" --receipt "$out/memory-reductions-rtl.json"
''
