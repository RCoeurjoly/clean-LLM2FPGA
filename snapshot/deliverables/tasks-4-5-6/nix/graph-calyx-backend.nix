{ runCommand, python3, calyx, fourStateVerifier }:

# Apply reviewed native guard/AST lowering and serializer source patches
# before compiling the backend. Generated RTL is never opened or rewritten.
calyx.overrideAttrs (old: {
  patches = (old.patches or []) ++ [
    ./patches/graph-calyx-component-file-emission.patch
    ./patches/graph-calyx-inferred-fsm-state-decode.patch
    ./patches/graph-calyx-deterministic-fsm-emission.patch
    ./patches/graph-calyx-signed-divider-sharing.patch
    ./patches/graph-calyx-bounded-priority.patch
    ./patches/graph-calyx-balanced-merged-guards.patch
    ./patches/graph-calyx-bounded-disjoint-checks.patch
    ./patches/graph-calyx-synthesis-check-dependencies.patch
    ./patches/graph-calyx-intern-guards.patch
    ./patches/graph-calyx-phase-logging.patch
    ./patches/graph-calyx-bounded-parser.patch
    ./patches/graph-calyx-live-control-ranges.patch
    ./patches/graph-calyx-unit-bound-conflicts.patch
    ./patches/graph-calyx-packed-conflicts.patch
    ./patches/graph-calyx-memory-stats.patch
    ./patches/graph-calyx-simplify-memory-stats.patch
    ./patches/graph-calyx-simplify-cleanup.patch
    ./patches/graph-calyx-component-cleanup.patch
    ./patches/graph-calyx-type-graph-lifetime.patch
    ./patches/graph-calyx-invoke-memory-stats.patch
    ./patches/graph-calyx-invoke-port-summary.patch
    ./patches/graph-calyx-invoke-timing.patch
    ./patches/graph-calyx-invoke-debug-snapshots.patch
    ./patches/graph-calyx-invoke-cleanup-schedule.patch
    ./patches/graph-calyx-static-promotion-diagnostics.patch
    ./patches/graph-calyx-promotion-cleanup.patch
    ./patches/graph-calyx-pass-memory-boundaries.patch
    ./patches/graph-calyx-late-comb-inputs.patch
  ];
  postPatch = (old.postPatch or "") + ''
    cp ${../compiler/native/calyx/LiveControlSet.rs} calyx/opt/src/analysis/live_control_set.rs
    cp ${../compiler/native/calyx/PackedConflictGraph.rs} calyx/utils/src/packed_conflict_graph.rs
    cp ${../compiler/native/calyx/CompilerMemory.rs} calyx/utils/src/compiler_memory.rs
    cp ${../compiler/native/calyx/LateCombInputs.rs} calyx/opt/src/passes/late_comb_inputs.rs
  '';
  postBuild = (old.postBuild or "") + ''
    rustc --edition=2024 --test calyx/opt/src/analysis/live_control_set.rs -o live-control-set-tests
    ./live-control-set-tests
    rustc --edition=2024 --test calyx/utils/src/compiler_memory.rs -o compiler-memory-tests
    ./compiler-memory-tests
    cargo test --offline --profile release -p calyx-utils packed_conflict
  '';
  postInstall = (old.postInstall or "") + ''
    mkdir -p signed-divider-tests/compiler/tests/calyx
    cp ${../tests/test_graph_calyx_signed_divider_sharing.py} signed-divider-tests/test_signed_divider.py
    cp ${../tests/test_graph_calyx_emission_reproducibility.py} signed-divider-tests/test_reproducibility.py
    cp ${../tests/test_graph_calyx_late_comb_inputs.py} signed-divider-tests/test_late_comb_inputs.py
    cp ${../compiler/tests/calyx/signed-divider-sharing.futil} signed-divider-tests/compiler/tests/calyx/signed-divider-sharing.futil
    cp ${../compiler/tests/calyx/signed-divider-parallel.futil} signed-divider-tests/compiler/tests/calyx/signed-divider-parallel.futil
    export LLM2FPGA_CALYX_TOOL="$out/bin/calyx"
    export LLM2FPGA_TEST_SUPPORT="$PWD/signed-divider-tests"
    export LLM2FPGA_IVERILOG="${fourStateVerifier}/bin/iverilog"
    ${python3}/bin/python3 -m unittest discover -s signed-divider-tests -p test_signed_divider.py -v
    ${python3}/bin/python3 -m unittest discover -s signed-divider-tests -p test_reproducibility.py -v
    ${python3}/bin/python3 -m unittest discover -s signed-divider-tests -p test_late_comb_inputs.py -v
  '';
  # cargoSetupHook copies this basename; its generated config refers to it.
  cargoDeps = runCommand "cargo-vendor-dir" {
    nativeBuildInputs = [ python3 ];
  } ''
    cp -rL ${old.cargoDeps} "$out"
    chmod -R u+w "$out"
    crate=$(find "$out" -maxdepth 2 -type d -name 'vast-0.3.3')
    test -n "$crate"
    patch -d "$crate" -p1 < ${./patches/graph-vast-concat-line-breaks.patch}
    patch -d "$crate" -p1 < ${./patches/graph-vast-preserve-binary-tree.patch}
    python - "$crate" <<'PY'
import hashlib
import json
import pathlib
import sys
crate = pathlib.Path(sys.argv[1])
path = crate / '.cargo-checksum.json'
checksum = json.loads(path.read_text())
# Cargo validates vendored source files independently of the pinned upstream
# package checksum. Refresh precisely the file changed by the source patch.
relative = 'src/subset/pretty_print.rs'
checksum['files'][relative] = hashlib.sha256((crate / relative).read_bytes()).hexdigest()
path.write_text(json.dumps(checksum, sort_keys=True))
PY
  '';
})
