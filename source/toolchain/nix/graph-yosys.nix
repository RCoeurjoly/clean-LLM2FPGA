# Patch the native compiler's mapping library, never emitted design source.
# The executable resolves its data directory relative to its own installed path.
{ lib, runCommand, coreutils, patch, python3, yosys, stdenv, iverilog }:
let
  boundedYosys = yosys.overrideAttrs (old: {
    patches = (old.patches or []) ++ [ ./patches/graph-yosys-bounded-gzip.patch
      ./patches/graph-yosys-deminout-no-inout.patch
      ./patches/graph-yosys-proc-prune-empty.patch
      ./patches/graph-yosys-connection-helpers.patch
      ./patches/graph-yosys-whole-wire-aliases.patch
      ./patches/graph-yosys-keyed-mux-grouping.patch
      ./patches/graph-yosys-consteval-lazy-mux.patch
      ./patches/graph-yosys-fsm-cached-bits.patch ];
    postBuild = (old.postBuild or "") + ''
      $CXX -std=c++17 -D_YOSYS_ -DYOSYS_ENABLE_ZLIB \
        -ffunction-sections -fdata-sections -I. \
        ${../tests/yosys_gzip_large_write.cpp} -Wl,--gc-sections -lz \
        -o gzip-large-write-test
      ./gzip-large-write-test gzip-large-write-test.gz
      ${python3}/bin/python - <<'PYTEST'
import gzip
count = 0
with gzip.open('gzip-large-write-test.gz', 'rb') as stream:
    for block in iter(lambda: stream.read(1048576), b""):
        assert block == b'x' * len(block)
        count += len(block)
assert count == 2147484672, count
PYTEST
      rm gzip-large-write-test gzip-large-write-test.gz
    '';
  });
  tests = lib.fileset.toSource {
    root = ../tests;
    fileset = lib.fileset.unions [
      ../tests/test_graph_xc7_mapping.py
      ../tests/test_graph_onchip_mapping.py
      ../tests/test_graph_yosys_deminout.py
      ../tests/test_graph_yosys_proc_prune.py
      ../tests/test_graph_yosys_connection_helpers.py
      ../tests/test_graph_yosys_whole_aliases.py
      ../tests/test_graph_yosys_mux_grouping.py
      ../tests/test_graph_yosys_consteval_mux.py
      ../tests/test_graph_yosys_fsm_opt.py
      ../tests/fixtures/fsm-opt
      ../tests/yosys_consteval_mux_probe.cpp
    ];
  };
  support = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/stages.py
      ../compiler/yosys_pipeline.py
      ../scripts/pipeline/map_graph_core_xc7.py
      ../scripts/pipeline/memory_policy.py
      ../rtl/primitives/closure_memory.sv
      ../rtl/primitives/closure_narrow_rom.sv
      ../calyx/primitives/closure_memory.futil
    ];
  };
in runCommand "llm2fpga-yosys-sdp72-parity" {
  nativeBuildInputs = [ coreutils patch python3 stdenv.cc iverilog ];
  passthru.upstream = yosys;
} ''
  mkdir -p "$out/bin" "$out/share"
  cp -R --no-preserve=ownership ${boundedYosys}/bin/. "$out/bin/"
  cp -R --no-preserve=ownership ${boundedYosys}/share/yosys "$out/share/"
  chmod -R u+w "$out/share/yosys"
  patch -d "$out/share/yosys" -p2 < ${./patches/graph-yosys-sdp72-upper-parity.patch}
  export PATH="$out/bin:$PATH"
  export PYTHONPATH=${support}
  export PYTHONDONTWRITEBYTECODE=1
  python -m unittest discover -s ${tests} -p test_graph_xc7_mapping.py -v
  python -m unittest discover -s ${tests} -p test_graph_onchip_mapping.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_deminout.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_proc_prune.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_connection_helpers.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_whole_aliases.py -v
  export LLM2FPGA_YOSYS_BASELINE="${yosys}/bin/yosys"
  python -m unittest discover -s ${tests} -p test_graph_yosys_mux_grouping.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_consteval_mux.py -v
  python -m unittest discover -s ${tests} -p test_graph_yosys_fsm_opt.py -v
''
