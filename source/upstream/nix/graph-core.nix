# Compile the actual model using only the filtered compiler and declared data.
# This is a core build; numerical and physical acceptance are downstream gates.
{ lib, runCommand, python, torchMlir, torchDriver, modelInputs
, recoverRestoringSqrtLoops ? false, coalescePointwisePingpong ? false, fuseGemvEpilogues ? false, poolPrivateCapacity ? false, stableScratchPooling ? false }:
assert builtins.isBool recoverRestoringSqrtLoops && builtins.isBool coalescePointwisePingpong;
assert builtins.isBool fuseGemvEpilogues && builtins.isBool poolPrivateCapacity;
assert builtins.isBool stableScratchPooling;
assert !stableScratchPooling || poolPrivateCapacity;
let
  compilerSource = import ./graph-core-source.nix { inherit lib; };
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  profile = {
    recover_restoring_sqrt_loops = recoverRestoringSqrtLoops;
    coalesce_pointwise_pingpong = coalescePointwisePingpong;
    fuse_gemv_epilogues = fuseGemvEpilogues;
    pool_private_capacity = poolPrivateCapacity;
    stable_scratch_pooling = stableScratchPooling;
  };
in runCommand "graph-core-tinystories-1m" {
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
    ${lib.optionalString recoverRestoringSqrtLoops "--recover-restoring-sqrt-loops"} \
    ${lib.optionalString coalescePointwisePingpong "--coalesce-pointwise-pingpong"} \
    ${lib.optionalString fuseGemvEpilogues "--fuse-gemv-epilogues"} \
    ${lib.optionalString poolPrivateCapacity "--pool-private-capacity"} \
    ${lib.optionalString stableScratchPooling "--stable-scratch-pooling"} \
    --timeout-seconds 7200
''
