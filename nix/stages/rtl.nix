{ calyx, coreutils, futil, runCommand }:
runCommand "rtl" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1800 '${calyx}/bin/calyx' \
    '${futil}/kernel.futil' \
    -b \
    verilog \
    -x \
    cell-share:bounds=1,-1,-1 \
    -x \
    cell-share:retire-dead-cells=true \
    -x \
    simplify-with-control:retire-dead-groups=true \
    -x \
    static-promotion:retire-dead-groups=true \
    -p \
    external \
    -l \
    '${calyx}/share/calyx' \
    --synthesis \
    -o \
    kernel.sv \
    -x \
    tdcc:infer-fsms=true
  test -s kernel.sv
  mkdir "$out"
  cp kernel.sv "$out/"
''
