{ core, coreutils, runCommand, torch }:
runCommand "structured" {
  nativeBuildInputs = [ coreutils ];
  OMP_NUM_THREADS = "1";
  OPENBLAS_NUM_THREADS = "1";
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  export HOME="$TMPDIR" XDG_CACHE_HOME="$TMPDIR/cache"
  mkdir work
  cd work
  timeout 300 '${torch}/bin/llm2fpga-torch-opt' \
    '${core}/controller/loops/loops.mlir' \
    --mlir-print-debuginfo \
    --verify-each \
    --arith-int-range-narrowing=int-bitwidths-supported=16,32,64 \
    --canonicalize \
    --canonicalize \
    -o \
    structured.mlir
  test -s structured.mlir
  mkdir "$out"
  cp structured.mlir "$out/"
''
