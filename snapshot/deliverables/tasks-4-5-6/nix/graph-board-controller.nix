# Open CH0 pad metadata and native controller emission; no model or oracle input.
# The controller still requires external CSR initialization and PHY calibration.
{ lib, runCommand, litedramPython, boardSource }:
let
  compilerSource = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/board_pins.py
      ../compiler/litedram_export.py
      ../compiler/stages.py
    ];
  };
in runCommand "graph-ypcb-ch0-litedram-controller" {
  nativeBuildInputs = [ litedramPython ];
  PYTHONPATH = compilerSource;
  PYTHONDONTWRITEBYTECODE = "1";
  passthru = { inherit compilerSource boardSource; };
} ''
  python - <<'PY'
  import os,sys
  from pathlib import Path
  import litedram,litex,migen
  from compiler.board_pins import export_board_pins_stage
  from compiler.litedram_export import export_controller_stage
  from compiler.stages import verify_stage_receipt

  root = Path(os.environ['out'])
  board = root / 'board-pins'
  python = Path(sys.executable)
  export_board_pins_stage(
      Path('${boardSource}/constraints/MEMORY_CH0.ucf'), board, python=python)
  sources = {name: Path(module.__file__).parent for name,module in
             [('litedram',litedram),('litex',litex),('migen',migen)]}
  export_controller_stage(board / 'pins.json', root / 'controller',
      python=python, sources=sources,
      input_dependencies=verify_stage_receipt(board))
  verify_stage_receipt(root / 'controller')
  PY
''
