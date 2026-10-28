# Pinned structural controller generator; does not patch emitted RTL.
{ pkgs, litexSource, litedramSource }:
let
  python = pkgs.python311;
  packages = pkgs.python311Packages;
  litex = packages.buildPythonPackage {
    pname = "llm2fpga-litex";
    version = "328da97ab916";
    src = litexSource;
    format = "setuptools";
    propagatedBuildInputs = with packages; [
      migen packaging pyserial pyyaml requests
    ];
    doCheck = false;
  };
  litedram = packages.buildPythonPackage {
    pname = "llm2fpga-litedram";
    version = "8bc71e8d006d";
    src = litedramSource;
    format = "setuptools";
    propagatedBuildInputs = [ litex packages.migen packages.pyyaml ];
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      ${python.interpreter} -m unittest \
        test.test_bankmachine test.test_crossbar test.test_adapter -v
      runHook postCheck
    '';
  };
in {
  inherit litex litedram;
  python = python.withPackages (_: [ litedram litex packages.migen packages.pyyaml ]);
}
