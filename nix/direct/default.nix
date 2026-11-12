{ inputs, system, tools
, quantizedRecipe ? ./quantized.nix
, checkRecipe ? ./check.nix
, arithmeticRecipe ? ./arithmetic.nix
, extraPythonPackages ? (ps: [])
}:
let
  pkgs = import inputs.nixpkgs-llvm21 {
    inherit system;
    config.allowUnfreePredicate = pkg: pkgs.lib.getName pkg == "torch";
  };
  python = pkgs.python311.withPackages (ps:
    [ ps.torch ps.transformers ] ++ extraPythonPackages ps);
  calibration = "${inputs.kev-gpt-src}/model_packages/tinystories-1m/calibration_ids.bin";
  snapshot = tools.model-snapshot;
  call = pkgs.lib.callPackageWith (pkgs // stages // {
    inherit python snapshot calibration;
    torchMlir = tools.graph-torch-mlir;
    mlir = tools.graph-torch-mlir-sdk;
    circt = tools.graph-circt;
    memory = tools.graph-calyx-opt;
    verifier = tools.graph-four-state-verifier;
    calyx = tools.graph-calyx-backend;
    yosys = tools.graph-yosys;
  });
  stages = {
    quantized = call quantizedRecipe {};
    torch-ir = call ./torch-ir.nix {};
    linalg = call ./linalg.nix {};
    loops = call ./loops.nix {};
    arithmetic = call arithmeticRecipe {};
    calyx = call ./calyx.nix {};
    futil = call ./futil.nix { inherit (stages) calyx; };
    core-rtl = call ./core-rtl.nix {};
    rtl = call ./rtl.nix { inherit (stages) calyx; };
    rtlil = call ./rtlil.nix {};
    prepared = call ./prepared.nix {};
    dsp = call ./dsp.nix {};
    ram = call ./ram.nix {};
    netlist = call ./netlist.nix {};
    float-calyx = call ./float-calyx.nix { calyx = tools.graph-calyx-backend; };
  };
in {
  inherit stages;
  check = call checkRecipe {};
  qparamsCheck = call ./qparams-check.nix {};
  memoryCheck = call ./memory-check.nix {};
  arithmeticCheck = call ./arithmetic-check.nix {};
}
