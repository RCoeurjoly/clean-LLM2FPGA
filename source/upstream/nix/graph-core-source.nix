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
    ../compiler/graph_images.py
    ../compiler/frontend/package_inputs.py
    ../compiler/frontend/package_model.py
    ../compiler/frontend/fixed_point_ops.py
    ../compiler/frontend/capture.py
    ../compiler/frontend/operator_support.json
    ../compiler/frontend/dependencies.json
    ../compiler/contracts/tinystories-1m-fixed-point.json
    ../TinyStories/serial_gemv_boundary.py
  ];
}
