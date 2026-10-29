{ pkgs, tools, fasm }:
let
  source = pkgs.lib.fileset.toSource {
    root = ../.;
    fileset = pkgs.lib.fileset.unions [
      ../build.py ../tools/bit_header.py ../tools/verify_decoded_frames.py
    ];
  };
in pkgs.runCommand "tinystories-1m-bitstream" {
  nativeBuildInputs = [ pkgs.python312 tools.prjxray tools.prjxray-python ];
  PYTHONPATH = pkgs.lib.concatStringsSep ":" [
    "${tools.fasm}/${pkgs.python312.sitePackages}"
    "${tools.prjxray-python}/${pkgs.python312.sitePackages}"
    "${tools.prjxray}/usr/share/python3"
  ];
  PRJXRAY_PYTHON_DIR = "${tools.prjxray}/usr/share/python3";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR"
  python3 ${source}/build.py ${fasm}/route.fasm ${tools.prjxray-db}/kintex7
  mkdir "$out"
  cp model.bit "$out/"
''
