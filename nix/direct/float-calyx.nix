{ runCommand, makeWrapper, patch, python, calyx }:
runCommand "calyx-float-primitives" {
  nativeBuildInputs = [ makeWrapper patch python ];
} ''
  mkdir -p "$out/share" "$out/bin"
  cp -r ${calyx}/share/calyx "$out/share/calyx"
  chmod -R u+w "$out/share/calyx"
  cp ${../../source/toolchain/rtl/primitives/float_div_sqrt.sv} \
    "$out/share/calyx/primitives/float/divSqrtFN.sv"
  cp ${../../source/toolchain/rtl/primitives/float_cast.sv} \
    "$out/share/calyx/primitives/float/float_cast.sv"
  cp ${../../source/toolchain/rtl/primitives/float_fma.sv} \
    "$out/share/calyx/primitives/float/float_fma.sv"
  # Normalize HardFloat's CRLF file before applying the LF source patch.
  python - "$out/share/calyx" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1]) / 'primitives/float/HardFloat-1/source/divSqrtRecFN_small.v'
path.write_text(path.read_text())
PY
  patch -d "$out/share/calyx" -p1 < \
    ${../../source/toolchain/nix/patches/calyx-float-division-handshake.patch}
  makeWrapper ${calyx}/bin/.calyx-wrapped "$out/bin/calyx" \
    --set CALYX_PRIMITIVES_DIR "$out/share/calyx"
''
