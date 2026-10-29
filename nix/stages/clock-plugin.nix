{ coreutils, runCommand, stdenv, yosys-upstream }:
runCommand "clock-plugin" {
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
    clock_divide2.so \
    '${../../snapshot/artifacts/task6/board-fit-2026-10-25/clock_divide2.cc}'
  test -s clock_divide2.so
  mkdir "$out"
  cp clock_divide2.so "$out/"
''
