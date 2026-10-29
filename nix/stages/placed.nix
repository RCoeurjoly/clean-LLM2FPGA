{ chipdb, coreutils, nextpnr, packed, python311, runCommand }:
runCommand "placed" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 5580 '${python311}/bin/python3' \
    '${../../snapshot/artifacts/task6/board-fit-2026-10-25/native_physical_runner.py}' \
    --input \
    '${packed}/design.json.gz' \
    --nextpnr \
    '${nextpnr}/bin/nextpnr-xilinx' \
    --chipdb \
    '${chipdb}/xc7k480tffg1156.bin' \
    --xdc \
    '${../../snapshot/deliverables/tasks-4-5-6/fpga/constraints/tinystories_1m_ypcb_bram_production.xdc}' \
    --phase \
    place \
    --timeout \
    5400
  test -s design.json.gz
  mkdir "$out"
  cp design.json.gz "$out/"
''
