# The dependency graph is model -> native stages -> verified bitstream.
{ pkgs, tools }:
let
  inherit (pkgs) lib;
  flow = builtins.fromJSON (builtins.readFile ../pipeline/flow.json);
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../build.py ../tools/bit_header.py ../tools/verify_decoded_frames.py
    ];
  };
  snapshot = builtins.path { path = ../snapshot; name = "fpga-inputs"; };
  core = tools.graph-core-1m-stable-scratch;
  # Old paths identify commands in the recorded flow, never store dependencies.
  toolPaths = {
    "/nix/store/597n2w1fmpslnd0iqzgln8fk9x4s0gdr-python3-3.11.14-env" = pkgs.python311;
    "/nix/store/xdzybdpp29qkji1szwlsz0mcqyidk2lh-llm2fpga-typed-torch-driver-0.1.0" = tools.graph-torch-opt;
    "/nix/store/gbk2cpjqx7pba2sl9wagygbk26rjxms1-llm2fpga-typed-calyx-driver-0.1.0" = tools.graph-calyx-opt;
    "/nix/store/b3m8c75hw6hhswbky443nfk0b1rm8fx3-calyx-0.7.1" = tools.graph-calyx-backend;
    "/nix/store/npgvbi2bx2w52q8saa350n3sbr99hg3v-llm2fpga-yosys-sdp72-parity" = tools.graph-yosys;
    "/nix/store/wwmfz02wyiy4gvav7n76jmhnpiyfj3q9-nextpnr-xilinx-kintex-bram-mode-fix-0.8.2" = tools.nextpnr-xilinx;
    "/nix/store/9rm81392klk28gvfaycd33ka8ckcd5sl-nextpnr-xilinx-chipdb-0.8.2" = tools.nextpnr-xilinx-chipdb;
    ${flow.core_store_path} = core;
    ${flow.source_store_path} = core.compilerSource;
  };
  # Match the most specific path first; replacing the checkout root first would
  # incorrectly send generated inputs to the static snapshot.
  rewrite = paths: text:
    let
      keys = lib.sort (a: b: builtins.stringLength a > builtins.stringLength b)
        (builtins.filter (key: lib.hasInfix key text) (builtins.attrNames paths));
    in builtins.replaceStrings keys (map (key: toString paths.${key}) keys) text;
  makeStage = state: stage:
    let
      oldDirectory = "${flow.original_root}/${stage.directory}";
      paths = toolPaths // state.paths // {
        ${flow.original_root} = snapshot;
        ${oldDirectory} = "@OUTPUT@";
      };
      generatedPath = name:
        assert lib.assertMsg (builtins.hasAttr name state.outputs)
          "Stage ${stage.name} requires an output not produced by an earlier stage: ${name}";
        state.outputs.${name};
      plugin = state.index < 2;
      # yosys-config supplies headers/ABI flags from the declared Yosys build.
      # Its g++ comes from stdenv.cc, not /usr/bin or the recorded host.
      argv = if plugin then [
        "${tools.graph-yosys.upstream}/bin/yosys-config"
        "--exec" "--cxx" "--cxxflags" "-shared" "-o"
        (builtins.head (builtins.attrNames stage.expected_outputs))
        (rewrite paths (lib.last stage.argv))
      ] else map (rewrite paths) stage.argv;
      spec = pkgs.writeText "stage-${toString state.index}.json" (builtins.toJSON ({
        inherit argv;
        inherit (stage) name expected_outputs timeout_seconds;
        inputs = map generatedPath (builtins.attrNames stage.generated_inputs);
        environment = lib.mapAttrs (_: rewrite paths) (stage.environment or {});
      } // lib.optionalAttrs (stage.stage_model_images or false) {
        model_images = toString core;
      } // lib.optionalAttrs (stage ? decompress_input) {
        decompress_input = generatedPath stage.decompress_input;
      }));
      result = pkgs.runCommand "bitstream-${toString state.index}-${stage.name}" {
        nativeBuildInputs = [ pkgs.python312 ] ++ lib.optional plugin pkgs.stdenv.cc;
        OMP_NUM_THREADS = "1";
        OPENBLAS_NUM_THREADS = "1";
        PYTHONDONTWRITEBYTECODE = "1";
        # Keep each stage's logs and outputs separately cacheable.
        passthru = { inherit spec; };
      } ''
        export HOME="$TMPDIR"
        export XDG_CACHE_HOME="$TMPDIR/cache"
        python3 ${source}/build.py stage ${spec} "$out"
        ${lib.optionalString plugin ''
          ${tools.graph-yosys.upstream}/bin/yosys -Q -T \
            -m "$out/${builtins.head (builtins.attrNames stage.expected_outputs)}" \
            -p 'help' > "$out/plugin-load.log"
        ''}
      '';
    in {
      index = state.index + 1;
      paths = state.paths // { ${oldDirectory} = result; };
      stages = state.stages ++ [ result ];
      outputs = state.outputs // lib.mapAttrs' (name: _: lib.nameValuePair
        "tree/${stage.directory}/${name}" "${result}/${name}") stage.expected_outputs;
    };
  graph = builtins.foldl' makeStage {
    index = 0; paths = {}; stages = [];
    outputs = lib.mapAttrs' (name: _: lib.nameValuePair "core/${name}" "${core}/${name}")
      flow.core.expected_outputs;
  } flow.stages;
  route = lib.last graph.stages;
  environment = {
    PATH = lib.makeBinPath [ tools.prjxray tools.prjxray-python pkgs.bash pkgs.coreutils ];
    PYTHONPATH = lib.concatStringsSep ":" [
      "${tools.fasm}/${pkgs.python312.sitePackages}"
      "${tools.prjxray-python}/${pkgs.python312.sitePackages}"
      "${tools.prjxray}/usr/share/python3"
    ];
    PRJXRAY_PYTHON_DIR = "${tools.prjxray}/usr/share/python3";
    PRJXRAY_DB_DIR = "${tools.prjxray-db}/kintex7";
  };
  assemblySpec = pkgs.writeText "bitstream-assembly.json" (builtins.toJSON {
    inherit environment;
    fasm = "${route}/route.fasm";
    script = "${snapshot}/deliverables/tasks-4-5-6/scripts/pipeline/build_and_validate_xc7_bitstream.sh";
  });
  bitstream = pkgs.runCommand "tinystories-1m-bitstream" {
    nativeBuildInputs = [ pkgs.python312 ];
    PYTHONDONTWRITEBYTECODE = "1";
    passthru = { inherit core route; stages = graph.stages; assemblyEnvironment = environment; };
  } ''
    export HOME="$TMPDIR"
    python3 ${source}/build.py assemble ${assemblySpec} "$out"
  '';
in {
  inherit bitstream route;
  mux-plugin = builtins.elemAt graph.stages 0;
  clock-plugin = builtins.elemAt graph.stages 1;
}
