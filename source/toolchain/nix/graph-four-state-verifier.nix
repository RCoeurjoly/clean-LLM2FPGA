{ runCommand, iverilog }:

# Verification-only backport of https://github.com/steveicarus/iverilog/pull/1412.
# The pinned v12 tool miscounts stale padding in inline bit-vector functions.
# Correct the simulator; emitted design RTL is never changed for this bug.
let
  package = iverilog.overrideAttrs (old: {
    patches = (old.patches or []) ++ [ ./patches/graph-iverilog-vpi-top-word.patch ];
    passthru = (old.passthru or {}) // {
      tests = runCommand "llm2fpga-four-state-verifier-tests" {} ''
        "${package}/bin/iverilog" -g2012 -s tb -o inline-counts.vvp \
          ${../compiler/tests/verilog/inline-bit-counts.sv}
        "${package}/bin/vvp" inline-counts.vvp
        touch "$out"
      '';
    };
  });
in package
