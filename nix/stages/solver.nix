{ clocked, coreutils, lib, python311, runCommand }:
let
  helperSource = lib.fileset.toSource {
    root = ../../snapshot;
    fileset = lib.fileset.unions [
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/select_native_router.py
      ../../snapshot/artifacts/task6/board-fit-2026-10-25/native_physical_runner.py
    ];
  };
in
runCommand "solver" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 780 '${python311}/bin/python3' \
    '${helperSource}/artifacts/task6/board-fit-2026-10-25/select_native_router.py' \
    --input \
    '${clocked}/design.json.gz' \
    --router \
    router2
  test -s design.json.gz
  mkdir "$out"
  cp design.json.gz "$out/"
''
