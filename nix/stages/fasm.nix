{ chipdb, coreutils, gzip, nextpnr, runCommand, solver }:
runCommand "fasm" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  ${gzip}/bin/gzip -dc ${solver}/design.json.gz > native-input.json
  timeout 43200 '${nextpnr}/bin/nextpnr-xilinx' \
    --chipdb \
    '${chipdb}/xc7k480tffg1156.bin' \
    --xdc \
    '${../../snapshot/deliverables/tasks-4-5-6/fpga/constraints/tinystories_1m_ypcb_bram_production.xdc}' \
    --freq \
    2.5 \
    --seed \
    15 \
    --no-tmdriv \
    --json \
    ./native-input.json \
    --no-pack \
    --no-place \
    --pre-route \
    '${../../snapshot/artifacts/task6/board-fit-2026-10-25/clock2p5_native_hook_v2.py}' \
    --fasm \
    ./route.fasm
  echo '7c4196526312282b67bfcb4052a2a256a10f45cbda93e64d90647e0d72af637c  route.fasm' | sha256sum -c -
  test -s route.fasm
  mkdir "$out"
  cp route.fasm "$out/"
''
