{ pkgs, bitstreamPkgs, openXC7Packages, nextpnr, route }:
let
  fasm = openXC7Packages.fasm;
  prjxray = openXC7Packages.prjxray;
  pythonDeps = bitstreamPkgs.python312.withPackages (ps: [
    ps.pyyaml ps.simplejson ps.intervaltree ps.pyjson5 ps.progressbar2
  ]);
  patchedDb = pkgs.runCommand "task6-bram-prjxray-db-lioi3-compat" {
    nativeBuildInputs = [ pkgs.patch ];
  } ''
    set -euo pipefail
    original=${nextpnr}/share/nextpnr/external/prjxray-db
    echo "d74bb512c56af57510ce89b351f1bf666e96196e20c8b1bc18e8521fe45bdd61  $original/kintex7/segbits_lioi3_tbytesrc.db" | sha256sum -c -
    echo '197e2f126a6f3917c1ceca62394acd957397679538df077f7119de97c28e05bc  ${../patches/prjxray-db/0001-kintex7-add-lioi3-tbytesrc-oclkm-imux31.patch}' | sha256sum -c -
    cp -rs "$original" "$out"
    chmod u+w "$out/kintex7"
    cp --remove-destination "$original/kintex7/segbits_lioi3_tbytesrc.db" "$out/kintex7/segbits_lioi3_tbytesrc.db"
    chmod u+w "$out/kintex7/segbits_lioi3_tbytesrc.db"
    cd "$out"
    patch --no-backup-if-mismatch -p1 < ${../patches/prjxray-db/0001-kintex7-add-lioi3-tbytesrc-oclkm-imux31.patch}
    echo '7108c7c92097d591e404852ce5eb18a6ed1851352d399eb29c1bd7eed3ffd710  kintex7/segbits_lioi3_tbytesrc.db' | sha256sum -c -
  '';
  dbRoot = "${patchedDb}/kintex7";
in pkgs.runCommand "task6-full16-two-rom-accepted-bitstream" {
  nativeBuildInputs = [ fasm prjxray pythonDeps pkgs.jq ];
} ''
  set -euo pipefail
  mkdir -p "$out"
  export PYTHONPATH="${fasm}/lib/python3.12/site-packages:${pythonDeps}/${bitstreamPkgs.python312.sitePackages}:${prjxray}/usr/share/python3"
  export PRJXRAY_PYTHON_DIR="${prjxray}/usr/share/python3"
  export PRJXRAY_DB_DIR="${dbRoot}"
  feature_hash=$(LC_ALL=C sort ${route}/routed.fasm | sha256sum | cut -d ' ' -f1)
  test "$feature_hash" = a97fea217ea3816d61d98ec2e8a1b5406bea0aa6482ceee95484edeb76866a8e
  timeout --kill-after=10s 600 bash ${../scripts/pipeline/build_and_validate_xc7_bitstream.sh} \
    ${route}/routed.fasm "$out/model.bit" "$out/model.frm" \
    ${dbRoot} ${dbRoot}/xc7k480tffg1156-1/part.yaml
  frame_hash=$(sha256sum "$out/model.frm" | cut -d ' ' -f1)
  decoded_hash=$(sha256sum "$out/model.bit.decoded-bits" | cut -d ' ' -f1)
  bitstream_hash=$(sha256sum "$out/model.bit" | cut -d ' ' -f1)
  test "$frame_hash" = c78a1ab40fea57c214194b95d989fa5ba5e4a32dbef1ea7a5a1595bde6931617
  test "$decoded_hash" = 2c3b88fc99f69e1914388e14ec3bcb5fcd5359e9c2d35c7e9ea81b88fe31cceb
  jq -n \
    --arg feature_hash "$feature_hash" \
    --arg frame_hash "$frame_hash" \
    --arg decoded_hash "$decoded_hash" \
    --arg bitstream_hash "$bitstream_hash" \
    --arg route ${route} \
    '{status:"source-rebuilt-accepted-task6-configuration",
      sorted_fasm_feature_sha256:$feature_hash,
      frames_sha256:$frame_hash,
      decoded_bits_sha256:$decoded_hash,
      rebuilt_bitstream_sha256:$bitstream_hash,
      archived_bitstream_sha256:"bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f",
      route:$route,
      limits:["Configuration frames and decoded bits match the archived board-tested image exactly; the .bit header embeds build path and time, so raw .bit SHA-256 differs.",
              "No new board run was performed.",
              "BRAM/DSP internal timing coverage remains incomplete."]}' \
    > "$out/receipt.json"
''
