{ runCommand, python, core-rtl, futil, calyx }:
runCommand "tinystories-rtl" { nativeBuildInputs = [ python ]; } ''
  python ${../../tools/bind_memories.py} ${futil}/model.futil \
    ${calyx}/memories.json "$out"
  ln -s ${core-rtl}/core.sv "$out/core.sv"
  cp ${../../rtl/model_memory.sv} "$out/memory.sv"
''
