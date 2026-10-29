{ runCommand, python312, core }:
runCommand "model-memory-bindings" {
    nativeBuildInputs = [ python312 ];
  } ''
    python3 - "${core}" "${../../snapshot/artifacts/task6/board-fit-2026-10-25/probe/bram-return-62-v1/bindings.json}" "$out" <<'PYTHON'
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
  ''
