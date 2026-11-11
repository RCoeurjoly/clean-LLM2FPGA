{ lib, rustPlatform, makeWrapper, calyxSrc, hardfloat }:

rustPlatform.buildRustPackage rec {
  pname = "calyx";
  version = "0.7.1";
  src = calyxSrc;
  nativeBuildInputs = [ makeWrapper ];
  cargoLock = {
    lockFile = "${calyxSrc}/Cargo.lock";
    extraRegistries = {
      "https://github.com/rust-lang/crates.io-index" =
        "https://static.crates.io/crates";
    };
    outputHashes = {
      "dap-0.4.1-alpha1" = "sha256-oJHeY9Hm8DMC1T9flRyjf6EmBcJc3tuvcPlZXtHTGqs=";
    };
  };

  preBuild = ''
    config="$NIX_BUILD_TOP/.cargo/config.toml"
    stanza='[source."https://github.com/rust-lang/crates.io-index"]'
    if ! test -f "$config"; then
      echo "generated Cargo configuration not found: $config" >&2
      exit 1
    fi
    echo "using generated Cargo configuration: $config"
    echo "checking for redundant crates.io source stanza in $config"
    if ! grep -Fqx "$stanza" "$config"; then
      echo "redundant crates.io source stanza is absent before cleanup" >&2
      exit 1
    fi
    tmp_config="$(mktemp "$config.XXXXXX")"
    awk '
      $0 == "[source.\"https://github.com/rust-lang/crates.io-index\"]" {
        if (found) {
          failed = 2
          exit
        }
        found = 1
        state = 1
        next
      }
      $0 ~ /^\[source\./ && state == 1 {
        state = 0
        print
        next
      }
      state != 1 { print }
      END {
        if (failed) exit failed
        if (found != 1) exit 3
      }
    ' "$config" > "$tmp_config"
    mv "$tmp_config" "$config"
    echo "removed redundant crates.io source stanza; checking it is absent"
    if grep -Fqx "$stanza" "$config"; then
      echo "redundant crates.io source stanza remains in $config" >&2
      exit 1
    fi
  '';

  CALYX_PRIMITIVES_DIR = "${placeholder "out"}/share/calyx";

  doCheck = false;

  postInstall = ''
    mkdir -p "$out/share/calyx"
    cp -r primitives "$out/share/calyx/"
    mkdir -p "$out/share/calyx/primitives/float/HardFloat-1"
    cp -r ${hardfloat}/. \
      "$out/share/calyx/primitives/float/HardFloat-1/"
    wrapProgram "$out/bin/calyx" \
      --set CALYX_PRIMITIVES_DIR "$out/share/calyx"
    test -x "$out/bin/calyx"
    test -f "$out/share/calyx/primitives/float/addFN.futil"
    test -f "$out/share/calyx/primitives/float/fpToInt.futil"
    test -f "$out/share/calyx/primitives/float/HardFloat-1/source/HardFloat_consts.vi"
  '';
}
