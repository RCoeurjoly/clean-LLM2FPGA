{ pkgs, tools }:
let
  call = pkgs.lib.callPackageWith (pkgs // stages // {
    core = tools.graph-core-1m-stable-scratch;
    torch = tools.graph-torch-opt;
    memory = tools.graph-calyx-opt;
    calyx = tools.graph-calyx-backend;
    yosys = tools.graph-yosys;
    yosys-upstream = tools.graph-yosys.upstream;
    nextpnr = tools.nextpnr-xilinx;
    chipdb = tools.nextpnr-xilinx-chipdb;
  });
  stages = {
    memory-inputs = call ./stages/memory-inputs.nix {};
    mux-plugin = call ./stages/mux-plugin.nix {};
    clock-plugin = call ./stages/clock-plugin.nix {};
    structured = call ./stages/structured.nix {};
    flat-memory = call ./stages/flat-memory.nix {};
    normalized-index = call ./stages/normalized-index.nix {};
    shared-functions = call ./stages/shared-functions.nix {};
    cse = call ./stages/cse.nix {};
    narrowed-loops = call ./stages/narrowed-loops.nix {};
    reduced-index = call ./stages/reduced-index.nix {};
    calyx-ir = call ./stages/calyx-ir.nix {};
    memory-contracts = call ./stages/memory-contracts.nix {};
    shared-logic = call ./stages/shared-logic.nix {};
    memory-validation = call ./stages/memory-validation.nix {};
    memory-binding = call ./stages/memory-binding.nix {};
    external-interface = call ./stages/external-interface.nix {};
    futil = call ./stages/futil.nix {};
    rtl = call ./stages/rtl.nix {};
    pre-share = call ./stages/pre-share.nix {};
    core-coarse = call ./stages/core-coarse.nix {};
    core-ram = call ./stages/core-ram.nix {};
    design-coarse = call ./stages/design-coarse.nix {};
    design-ram = call ./stages/design-ram.nix {};
    mapped = call ./stages/mapped.nix {};
    divided-clock = call ./stages/divided-clock.nix {};
    mux-lowered = call ./stages/mux-lowered.nix {};
    netlist = call ./stages/netlist.nix {};
    packed = call ./stages/packed.nix {};
    placed = call ./stages/placed.nix {};
    clocked = call ./stages/clocked.nix {};
    solver = call ./stages/solver.nix {};
    fasm = call ./stages/fasm.nix {};
  };
in stages
