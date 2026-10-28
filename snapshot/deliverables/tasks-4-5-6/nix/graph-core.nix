# Compile the actual model using only the filtered compiler and declared data.
# This is a core build; numerical and physical acceptance are downstream gates.
{ lib, runCommand, python, torchMlir, torchDriver, circt, memoryDriver, calyx, modelInputs
, memoryImplementation ? "bram", narrowImmutable ? true, offchipImmutable ? false, recoverRestoringSqrtLoops ? false, coalescePointwisePingpong ? false, fuseGemvEpilogues ? false, poolPrivateCapacity ? false, stableScratchPooling ? false, lutMemoryMaxBits ? 0 }:
assert builtins.elem memoryImplementation [ "register" "bram" "lutram" ];
assert builtins.isBool narrowImmutable && builtins.isBool offchipImmutable;
assert builtins.isBool recoverRestoringSqrtLoops && builtins.isBool coalescePointwisePingpong;
assert builtins.isBool fuseGemvEpilogues && builtins.isBool poolPrivateCapacity;
assert builtins.isBool stableScratchPooling;
assert !stableScratchPooling || poolPrivateCapacity;
assert builtins.isInt lutMemoryMaxBits && lutMemoryMaxBits >= 0;
assert lutMemoryMaxBits == 0 || memoryImplementation == "bram";
assert !(offchipImmutable && narrowImmutable);
let
  compilerSource = import ./graph-core-source.nix { inherit lib; };
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  profile = {
    memory_implementation = memoryImplementation;
    narrow_immutable = narrowImmutable;
    offchip_immutable = offchipImmutable;
    recover_restoring_sqrt_loops = recoverRestoringSqrtLoops;
    coalesce_pointwise_pingpong = coalescePointwisePingpong;
    fuse_gemv_epilogues = fuseGemvEpilogues;
    pool_private_capacity = poolPrivateCapacity;
    stable_scratch_pooling = stableScratchPooling;
    lut_memory_max_bits = lutMemoryMaxBits;
  };
in runCommand "graph-core-tinystories-1m${lib.optionalString offchipImmutable "-ddr"}" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONPATH = "${compilerSource}:${torchMlir}/${python.sitePackages}:${torchMlir}/${python.sitePackages}/torch_mlir";
  passthru = { inherit compilerSource modelInputs profile; };
} ''
  python -m compiler.compile_core \
    --snapshot ${modelInputs.snapshot} \
    --package ${modelInputs.package} \
    --numerical-contract ${compilerSource}/compiler/contracts/tinystories-1m-fixed-point.json \
    --output "$out" \
    --torch-tool ${torchDriver}/bin/llm2fpga-torch-opt \
    --circt-tool ${circt}/bin/circt-opt \
    --memory-tool ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate-tool ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx-tool ${calyx}/bin/calyx \
    --calyx-library ${calyx}/share/calyx \
    --memory-implementation ${memoryImplementation} \
    ${if narrowImmutable then "--narrow-immutable" else "--no-narrow-immutable"} \
    ${lib.optionalString offchipImmutable "--offchip-immutable"} \
    ${lib.optionalString recoverRestoringSqrtLoops "--recover-restoring-sqrt-loops"} \
    ${lib.optionalString coalescePointwisePingpong "--coalesce-pointwise-pingpong"} \
    ${lib.optionalString fuseGemvEpilogues "--fuse-gemv-epilogues"} \
    ${lib.optionalString poolPrivateCapacity "--pool-private-capacity"} \
    ${lib.optionalString stableScratchPooling "--stable-scratch-pooling"} \
    --lut-memory-max-bits ${toString lutMemoryMaxBits} \
    --timeout-seconds 7200
''
