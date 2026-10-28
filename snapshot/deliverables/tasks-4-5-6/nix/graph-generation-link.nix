# Runtime feedback and typed compiler-boundary checks, not full model acceptance.
{ lib, runCommand, python, torchDriver, torchMlir, circt, memoryDriver }:
let
  pythonEnv = python.withPackages (ps: [ ps.numpy ]);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../compiler/pipeline.py ../compiler/stages.py ../compiler/generation_driver.py
      ../tests/test_graph_generation_link.py ../tests/test_graph_generation_abi.py
      ../tests/test_graph_generation_driver.py
    ];
  };
in runCommand "graph-generation-link-check" {
  nativeBuildInputs = [ pythonEnv ];
  PYTHONPATH = "${source}:${torchMlir}/${python.sitePackages}/torch_mlir";
  PYTHONDONTWRITEBYTECODE = "1";
  LLM2FPGA_TORCH_PASS_TOOL = "${torchDriver}/bin/llm2fpga-torch-opt";
  LLM2FPGA_CIRCT_TOOL = "${circt}/bin/circt-opt";
  LLM2FPGA_MEMORY_TOOL = "${memoryDriver}/bin/llm2fpga-calyx-opt";
} ''
  mkdir -p "$out"
  python -m unittest discover -s ${source}/tests -p test_graph_generation_link.py -v \
    > "$out/link-tests.log" 2>&1
  python -m unittest discover -s ${source}/tests -p test_graph_generation_abi.py -v \
    > "$out/abi-tests.log" 2>&1
  python -m unittest discover -s ${source}/tests -p test_graph_generation_driver.py -v \
    > "$out/driver-tests.log" 2>&1
''
