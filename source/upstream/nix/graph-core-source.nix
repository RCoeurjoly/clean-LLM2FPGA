# Compilation-only source bundle. Keep verification and historical generators out.
{ lib }:
lib.fileset.toSource {
  root = ../.;
  fileset = lib.fileset.unions [
    ../compiler/build_core.py
    ../compiler/compile_core.py
    ../compiler/build_profile.py
    ../compiler/stages.py
    ../compiler/pipeline.py
    ../compiler/generation.py
    ../compiler/generation_driver.py
    ../compiler/litedram_export.py
    ../compiler/ddr_transport.py
    ../compiler/clock_contract.py
    ../compiler/ddr_bank_adapter.py
    ../compiler/ddr_images.py
    ../compiler/graph_images.py
    ../compiler/memory_plan.py
    ../compiler/frontend/package_inputs.py
    ../compiler/frontend/package_model.py
    ../compiler/frontend/fixed_point_ops.py
    ../compiler/frontend/capture.py
    ../compiler/frontend/operator_support.json
    ../compiler/frontend/dependencies.json
    ../compiler/contracts/tinystories-1m-fixed-point.json
    ../TinyStories/serial_gemv_boundary.py
    ../scripts/pipeline/memory_policy.py
    ../rtl/primitives/closure_ddr_read_port.sv
    ../rtl/primitives/closure_ddr_read_bank.sv
    ../rtl/primitives/closure_ddr_read_cdc.sv
    ../rtl/primitives/closure_ddr_image_loader.sv
    ../rtl/primitives/closure_ddr_loaded_port.sv
    ../rtl/primitives/closure_memory.sv
    ../rtl/primitives/closure_narrow_rom.sv
    ../calyx/primitives/closure_memory.futil
    ./graph-core-source.nix
    ./graph-core-data.nix
    ./graph-core.nix
  ];
}
