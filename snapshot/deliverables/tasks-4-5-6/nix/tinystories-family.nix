{ pkgs, tinyStories1m }:
let
  # Independently prefetched at each of the five pinned model revisions. All
  # five repositories carry byte-identical GPT-2 tokenizer files.
  tokenizerHashes = {
    "merges.txt" = "sha256-HOFmR3PFDz4MyIQmGak+3EYkUltyixiKngvjO3cmrcU=";
    "vocab.json" = "sha256-O6PDEJ/zOXbEvZZlicEe4U/KofTJ5eFUwu1/mdgHCec=";
    "tokenizer.json" = "sha256-9u09MHAQwkTCKu/73gX0Gc8nfCPmTPmLZzysVEnP7/U=";
    "tokenizer_config.json" = "sha256-PXbaD9N0k/v80/D6l1d1PTH5Lhd569kTCAm0VUamAmE=";
    "special_tokens_map.json" = "sha256-mEEhN65Dx3+K9S61GxnDU20yQstVM5Fn2EEAX6lKI7c=";
  };
  mkModel = { size, revision, configHash ? null, weightsHash ? null
            , configSha256, weightsSha256, baselineSnapshot ? null }:
    let
      modelId = "roneneldan/TinyStories-${size}";
      fetch = file: hash: pkgs.fetchurl {
        url = "https://huggingface.co/${modelId}/resolve/${revision}/${file}";
        inherit hash;
      };
      config = if baselineSnapshot == null then fetch "config.json" configHash
               else "${baselineSnapshot}/config.json";
      weights = if baselineSnapshot == null then fetch "pytorch_model.bin" weightsHash
                else "${baselineSnapshot}/pytorch_model.bin";
      tokenizerEntries = pkgs.lib.mapAttrsToList (name: hash: {
        inherit name;
        path = fetch name hash;
      }) tokenizerHashes;
    in {
      inherit modelId revision;
      inherit configSha256 weightsSha256;
      snapshot = pkgs.linkFarm "task5-tinystories-${size}-snapshot" ([
        { name = "config.json"; path = config; }
        { name = "pytorch_model.bin"; path = weights; }
      ] ++ tokenizerEntries);
    };
in {
  "1M" = mkModel {
    size = "1M";
    inherit (tinyStories1m) revision;
    baselineSnapshot = tinyStories1m.snapshot;
    configSha256 = "ff74c30d5ebb5ab1da0f2ea479adf7197c504b42b5522a858c334ab91ed4958c";
    weightsSha256 = "07f9609ea882b8163ff3b23d40e2b82cb715d409631beb15c84b164f3877dae7";
  };
  "3M" = mkModel {
    size = "3M";
    revision = "cfaf26ec85ecdfc1bd7c2638104cce55cb67f894";
    configHash = "sha256-hGl4a/W3iQVkNIRbBrj05B4totrpQF/ehSFVcf9QY/4=";
    weightsHash = "sha256-Dk+TqGQHo/aFUg5ISBojbHteOk+OT8jR5Y86dI2Xo3w=";
    configSha256 = "8469786bf5b789056434845b06b8f4e41e2da2dae9405fde85215571ff5063fe";
    weightsSha256 = "0e4f93a86407a3f685520e48481a236c7b5e3a4f8e4fc8d1e58f3a748d97a37c";
  };
  "8M" = mkModel {
    size = "8M";
    revision = "8612e3b15c66ffa94eaa6ee0de5c96edd2d630af";
    configHash = "sha256-X/FrA760RmveUgRpqBWi1DnhaJZlXRFRw7RGhrOHpC0=";
    weightsHash = "sha256-IsNVv6vrwfbIYbP116gB6Wx/baSvS7D3eACWq4LqZxY=";
    configSha256 = "5ff16b03beb4466bde520469a815a2d439e16896655d1151c3b44686b387a42d";
    weightsSha256 = "22c355bfabebc1f6c861b3f5d7a801e96c7f6da4af4bb0f7780096ab82ea6716";
  };
  "28M" = mkModel {
    size = "28M";
    revision = "52dabea9997faf578489d619249616926e54ed18";
    configHash = "sha256-o1pFt1oIeoWbqFNpp3IaIpgTgQxsgFlkH+dT57U67tE=";
    weightsHash = "sha256-jd0mD1G0OXRMjMeFtVFjJ9S/MuMcy/qQCb+t8SVX/PU=";
    configSha256 = "a35a45b75a087a859ba85369a7721a229813810c6c8059641fe753e7b53aeed1";
    weightsSha256 = "8ddd260f51b439744c8cc785b5516327d4bf32e31ccbfa9009bfadf12557fcf5";
  };
  "33M" = mkModel {
    size = "33M";
    revision = "2ad0a164221b7c4d21cac7c46aec74f6f98dbfc8";
    configHash = "sha256-yC8lFxsMrt5hykLHKCCVU/iqGcF8AsG0+bjTx3rwvpQ=";
    weightsHash = "sha256-QTFtO60893ZsvDZEOnHF7wUyGhxPoUhV31N5mMba0wI=";
    configSha256 = "c82f25171b0caede61ca42c728209553f8aa19c17c02c1b4f9b8d3c77af0be94";
    weightsSha256 = "41316d3bad3cf7766cbc36443a71c5ef05321a1c4fa14855df537998c6dad302";
  };
}
