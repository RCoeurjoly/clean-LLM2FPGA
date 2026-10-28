# Copy only declared model inputs. The compiler depends on these outputs,
# rather than the original repository containing verification artifacts.
{ runCommand, snapshot, package }:
{
  snapshot = runCommand "graph-core-model-snapshot" {} ''
    mkdir -p "$out"
    cp ${snapshot}/config.json "$out/config.json"
    cp ${snapshot}/pytorch_model.bin "$out/pytorch_model.bin"
  '';
  package = runCommand "graph-core-model-package" {} ''
    mkdir -p "$out"
    cp ${package}/manifest.json "$out/manifest.json"
    cp ${package}/weights.bin "$out/weights.bin"
    cp ${package}/scales.bin "$out/scales.bin"
  '';
}
