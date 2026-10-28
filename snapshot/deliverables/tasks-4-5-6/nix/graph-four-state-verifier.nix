{ iverilog }:

# Verification-only backport of https://github.com/steveicarus/iverilog/pull/1412.
# The pinned v12 tool miscounts stale padding in inline bit-vector functions.
# Correct the simulator; emitted design RTL is never changed for this bug.
iverilog.overrideAttrs (old: {
  patches = (old.patches or []) ++ [ ./patches/graph-iverilog-vpi-top-word.patch ];
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/iverilog" -g2012 -s tb -o inline-counts.vvp \
      ${../compiler/tests/verilog/inline-bit-counts.sv}
    "$out/bin/vvp" inline-counts.vvp
    runHook postInstallCheck
  '';
})
