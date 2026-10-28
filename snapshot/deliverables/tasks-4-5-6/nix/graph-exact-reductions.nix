# Focused native RTL gate for the selected exact compiler reductions.
{ lib, runCommand, stdenv, gnumake, verilator, python, torchDriver, memoryDriver, calyx }:
let
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py
      ../scripts/comparison/verify_graph_exact_reductions_rtl.py
    ];
  };
in runCommand "graph-exact-reductions-rtl-check" {
  nativeBuildInputs = [ python stdenv.cc gnumake verilator ];
  PYTHONDONTWRITEBYTECODE = "1";
} ''
  mkdir -p "$out"
  python ${source}/scripts/comparison/verify_graph_exact_reductions_rtl.py \
    --torch ${torchDriver}/bin/llm2fpga-torch-opt \
    --memory ${memoryDriver}/bin/llm2fpga-calyx-opt \
    --translate ${memoryDriver}/bin/llm2fpga-calyx-translate \
    --calyx ${calyx}/bin/calyx --calyx-library ${calyx}/share/calyx \
    --verilator ${verilator}/bin/verilator \
    --work-dir "$out/work" --receipt "$out/exact-reductions-rtl.json"
  python - "$out/exact-reductions-rtl.json" ${verilator}/bin/verilator ${source}/compiler/stages.py <<'PY'
import hashlib, json, pathlib, sys
receipt = json.loads(pathlib.Path(sys.argv[1]).read_text())
for name in sys.argv[2:]:
    assert receipt['dependency_hashes'].get(name) == hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest(), name
PY
''
