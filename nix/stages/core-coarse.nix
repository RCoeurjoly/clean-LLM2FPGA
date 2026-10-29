{ coreutils, pre-share, runCommand, yosys }:
runCommand "core-coarse" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 7200 '${yosys}/bin/yosys' \
    -p \
    '
      read_rtlil "${pre-share}/pre-share.il.gz";
      hierarchy -check -top main;
      share -fast -pattern-limit 64;
      opt;
      memory -nomap;
      opt_clean;
      write_rtlil checkpoint.il.gz
    '
  test -s checkpoint.il.gz
  mkdir "$out"
  cp checkpoint.il.gz "$out/"
''
