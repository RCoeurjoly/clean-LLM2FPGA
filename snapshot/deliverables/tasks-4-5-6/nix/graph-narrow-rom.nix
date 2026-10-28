# Verification only: native certificate checks, RTL reads and compact storage.
{ lib, runCommand, python3, stdenv, gnumake, verilator, yosys, memoryDriver, calyx }:
let
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py
      ../scripts/pipeline/memory_policy.py
      ../scripts/comparison/verify_graph_narrow_rom_rtl.py
      ../rtl/primitives/closure_memory.sv ../rtl/primitives/closure_narrow_rom.sv
      ../calyx/primitives/closure_memory.futil
    ];
  };
in runCommand "graph-narrow-rom-check" {
  nativeBuildInputs = [ python3 stdenv.cc gnumake verilator yosys ];
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  mkdir -p "$out"
  python ${source}/scripts/comparison/verify_graph_narrow_rom_rtl.py \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator --yosys ${yosys}/bin/yosys \
    --work-dir "$out/work" --receipt "$out/narrow-rom.json" \
    > "$out/tests.log" 2>&1 || { cat "$out/tests.log"; exit 1; }
''
