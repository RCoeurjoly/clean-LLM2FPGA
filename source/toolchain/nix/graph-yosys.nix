{ runCommand, coreutils, patch, yosys }:
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

  });
  package = runCommand "llm2fpga-yosys-sdp72-parity" {
    nativeBuildInputs = [ coreutils patch ];
  } ''
    mkdir -p "$out/bin" "$out/share"
    cp -R --no-preserve=ownership ${boundedYosys}/bin/. "$out/bin/"
    cp -R --no-preserve=ownership ${boundedYosys}/share/yosys "$out/share/"
    chmod -R u+w "$out/share/yosys"
    patch -d "$out/share/yosys" -p2 < ${./patches/graph-yosys-sdp72-upper-parity.patch}
  '';
in package
