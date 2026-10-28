{ fetchurl, stdenvNoCC, unzip }:

stdenvNoCC.mkDerivation rec {
  pname = "hardfloat";
  version = "1";

  src = fetchurl {
    url = "http://www.jhauser.us/arithmetic/HardFloat-1.zip";
    hash = "sha256-azdXyfv6IjDGorhGBeOTcstYnddQDpecTwuOzIoDsUs=";
  };

  nativeBuildInputs = [ unzip ];
  dontConfigure = true;

  unpackPhase = ''
    unzip -q "$src"
  '';

  installPhase = ''
    mkdir -p "$out"
    cp -r HardFloat-1/. "$out/"
    test -f "$out/source/HardFloat_consts.vi"
    test -f "$out/source/RISCV/HardFloat_specialize.vi"
  '';
}
