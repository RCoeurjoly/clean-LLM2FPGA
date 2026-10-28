# Kernel gate for the shared graph profile. Production model builds are separate.
{ lib, runCommand, stdenv, gnumake, verilator, yosys, python, torchMlir, torchDriver
, circt, memoryDriver, calyx, task2Source }:
let
  pythonEnv = python.withPackages (ps: [ ps.torch ps.numpy ]);
  compilerSource = lib.cleanSourceWith {
    src = ../.;
    name = "graph-kernel-compiler-source";
    filter = path: type:
      let relative = lib.removePrefix "${toString ../.}/" (toString path);
      in path == toString ../.
        || relative == "compiler" || lib.hasPrefix "compiler/" relative
        || builtins.elem relative [
          "TinyStories" "TinyStories/serial_gemv_boundary.py"
          "scripts" "scripts/comparison"
          "scripts/comparison/verify_graph_kernel_rtl.py"
          "scripts/comparison/verify_graph_initialized_rtl.py"
          "scripts/pipeline" "scripts/pipeline/memory_policy.py"
          "rtl" "rtl/primitives" "rtl/primitives/closure_memory.sv"
          "calyx" "calyx/primitives" "calyx/primitives/closure_memory.futil"
        ];
  };
in runCommand "graph-kernel-rtl-check" {
  nativeBuildInputs = [ pythonEnv stdenv.cc gnumake verilator ];
  PYTHONDONTWRITEBYTECODE = "1";
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONPATH = "${torchMlir}/${python.sitePackages}:${torchMlir}/${python.sitePackages}/torch_mlir";
} ''
  mkdir -p "$out"
  python ${compilerSource}/scripts/comparison/verify_graph_kernel_rtl.py \
    --torch ${torchDriver}/bin/llm2fpga-torch-opt \
    --circt ${circt}/bin/circt-opt \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx \
    --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator \
    --task2-source ${task2Source} \
    --work-dir "$out/work" --receipt "$out/kernel-rtl.json"
  python ${compilerSource}/scripts/comparison/verify_graph_initialized_rtl.py \
    --torch ${torchDriver}/bin/llm2fpga-torch-opt \
    --circt ${circt}/bin/circt-opt \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx \
    --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator \
    --yosys ${yosys}/bin/yosys \
    --work-dir "$out/initialized" --receipt "$out/initialized-kernel-rtl.json"
  python ${compilerSource}/scripts/comparison/verify_graph_initialized_rtl.py \
    --composed \
    --torch ${torchDriver}/bin/llm2fpga-torch-opt \
    --circt ${circt}/bin/circt-opt \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx \
    --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator \
    --yosys ${yosys}/bin/yosys \
    --work-dir "$out/composed" --receipt "$out/composed-kernel-rtl.json"
''
