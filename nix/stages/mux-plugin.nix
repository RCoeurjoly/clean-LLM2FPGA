{ coreutils, runCommand, stdenv, yosys-upstream }:
runCommand "mux-plugin" {
  nativeBuildInputs = [ coreutils stdenv.cc ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${yosys-upstream}/bin/yosys-config' \
    --exec \
    --cxx \
    --cxxflags \
    -shared \
    -o \
    mux_to_lut.so \
    '${../../snapshot/artifacts/task6/board-fit-2026-10-25/mux_to_lut.cc}'
  test -s mux_to_lut.so
  mkdir "$out"
  cp mux_to_lut.so "$out/"
''
