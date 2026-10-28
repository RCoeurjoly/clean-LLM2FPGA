{ pkgs, python, family, source, packageLibrary, acceptedPackage }:
let
  mkPackage = size:
    let
      model = family.${size};
      isBaseline = size == "1M";
    in pkgs.runCommand "task5-tinystories-${size}-fixed-point-package" {
      nativeBuildInputs = [ python ];
    } ''
      export PYTHONPATH=${source}:${packageLibrary}
      mkdir -p "$out"
      python ${source}/scripts/pipeline/build_tinystories_family_package.py \
        --snapshot ${model.snapshot} --revision ${model.revision} \
        --calibration-ids ${acceptedPackage}/calibration_ids.bin \
        ${if isBaseline then "--activation-scales-json ${acceptedPackage}/manifest.json" else ""} \
        --output "$out"
      ${if isBaseline then ''
        cmp "$out/weights.bin" ${acceptedPackage}/weights.bin
        cmp "$out/scales.bin" ${acceptedPackage}/scales.bin
        cmp "$out/calibration_ids.bin" ${acceptedPackage}/calibration_ids.bin
        cmp "$out/manifest.json" ${acceptedPackage}/manifest.json
      '' else ""}
      python -c 'import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); m=json.loads((p/"manifest.json").read_text()); print(json.dumps({"schema":"task5-fixed-point-family-package-v1","variant":"proven-fixed-point-family","size_label":sys.argv[2],"source_revision":m["model"]["source_revision"],"quantization":"per-output-int8-weights-symmetric-int8-activations","calibration_sha256":m["calibration"]["token_ids_sha256"],"activation_boundaries":len(m["activation_scales"]),"numeric_contract":"Q16.16/Q8.24/signed-64-wrapping-MAC","baseline_numeric_payload_byte_identical":sys.argv[2]=="1M","tokenizer_included":all((p/name).is_file() for name in ("merges.txt","vocab.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json"))},sort_keys=True,indent=2))' "$out" ${size} > "$out/family-stage.json"
    '';
in builtins.listToAttrs (map (size: {
  name = size;
  value = mkPackage size;
}) [ "1M" "3M" "8M" "28M" "33M" ])
