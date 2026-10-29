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
  bindingManifest = "artifacts/task6/board-fit-2026-10-25/probe/bram-return-62-v1/bindings.json";
  # The compiler resolves initializer filenames relative to the manifest.
  bindingInputs = pkgs.runCommand "model-memory-bindings" {
    nativeBuildInputs = [ pkgs.python312 ];
  } ''
    python3 - "${core}" "${snapshot}/${bindingManifest}" "$out" <<'PYTHON'
import hashlib, json, shutil, sys
from pathlib import Path
core, manifest, output = map(Path, sys.argv[1:])
output.mkdir()
shutil.copyfile(manifest, output / 'bindings.json')
images = {}
for binding in json.loads(manifest.read_text())['bindings']:
    name = binding.get('init_file')
    if name is None:
        continue
    if Path(name).name != name:
        raise ValueError('Initializer must be a local filename: ' + name)
    expected = binding['init_sha256']
    if name in images and images[name] != expected:
        raise ValueError('Conflicting initializer hashes: ' + name)
    images[name] = expected
for name, expected in images.items():
    source = core / name
    with source.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError('Generated initializer identity mismatch: ' + name)
    (output / name).symlink_to(source)
print(f'Verified {len(images)} model initializer files')
PYTHON
  '';
  # Symbolic references resolve only to declared Nix dependencies.
  toolPaths = {
    "@python@" = pkgs.python311;
    "@torch@" = tools.graph-torch-opt;
    "@memory@" = tools.graph-calyx-opt;
    "@calyx@" = tools.graph-calyx-backend;
    "@yosys@" = tools.graph-yosys;
    "@nextpnr@" = tools.nextpnr-xilinx;
    "@chipdb@" = tools.nextpnr-xilinx-chipdb;
    "@core@" = core;
    "@tree@/${bindingManifest}" = "${bindingInputs}/bindings.json";
    "@yosys-config@" = tools.graph-yosys.upstream;
  };
  # Match the most specific path first; replacing the checkout root first would
  # incorrectly send generated inputs to the static snapshot.
  rewrite = paths: text:
    let
      keys = lib.sort (a: b: builtins.stringLength a > builtins.stringLength b)
        (builtins.filter (key: lib.hasInfix key text) (builtins.attrNames paths));
      resolved = builtins.replaceStrings keys (map (key: toString paths.${key}) keys) text;
    in assert lib.assertMsg
      (!(lib.hasInfix "@" (builtins.replaceStrings [ "@OUTPUT@" ] [ "" ] resolved)))
      "Unresolved build input reference: ${text}";
      resolved;
  makeStage = state: stage:
    let
      oldDirectory = "@tree@/${stage.directory}";
      paths = toolPaths // state.paths // {
        "@tree@" = snapshot;
        ${oldDirectory} = "@OUTPUT@";
      };
      generatedPath = name:
        assert lib.assertMsg (builtins.hasAttr name state.outputs)
          "Stage ${stage.name} requires an output not produced by an earlier stage: ${name}";
        state.outputs.${name};
      plugin = state.index < 2;
      # yosys-config supplies headers/ABI flags from the declared Yosys build.
      # Its g++ comes from stdenv.cc, not /usr/bin or the recorded host.
      argv = map (rewrite paths) stage.argv;
      spec = pkgs.writeText "stage-${toString state.index}.json" (builtins.toJSON ({
        inherit argv;
        inherit (stage) name expected_outputs timeout_seconds;
        inputs = map generatedPath stage.generated_inputs;
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
    outputs = builtins.listToAttrs (map (name: lib.nameValuePair "core/${name}" "${core}/${name}")
      flow.core_outputs);
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
