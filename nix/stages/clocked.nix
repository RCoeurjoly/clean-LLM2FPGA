{ chipdb, coreutils, lib, nextpnr, placed, python311, runCommand }:
let
  helperSource = lib.fileset.toSource {
    root = ../../snapshot;
    fileset = lib.fileset.unions [
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/native_divided_clock_runner.py
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/native_physical_runner.py
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/clock2p5_policy.py
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/clock5_policy.py
    ];
  };
in
runCommand "clocked" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 1980 '${python311}/bin/python3' \
    '${helperSource}/artifacts/task6/board-fit-2026-10-25/native_divided_clock_runner.py' \
    --input \
    '${placed}/design.json.gz' \
    --nextpnr \
    '${nextpnr}/bin/nextpnr-xilinx' \
    --chipdb \
    '${chipdb}/xc7k480tffg1156.bin' \
    --xdc \
    '${../../snapshot/deliverables/tasks-4-5-6/fpga/constraints/tinystories_1m_ypcb_bram_production.xdc}' \
    --phase \
    clock \
    --timeout \
    1800 \
    --hook \
    '${../../snapshot/artifacts/task6/board-fit-2026-10-25/clock2p5_native_hook_v2.py}'
  test -s design.json.gz
  mkdir "$out"
  cp design.json.gz "$out/"
''
