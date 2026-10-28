#!/usr/bin/env python3
"""Execute the pure model with only its declared compilation dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink():
        raise ValueError('isolation report must not be a symlink')
    output = args.output.resolve()
    protected = (args.snapshot.resolve(), args.package.resolve(),
                 *(ROOT / name for name in ('compiler', 'TinyStories', 'scripts',
                   'nix', 'rtl', 'calyx', 'model_inputs', 'flake.nix', 'flake.lock')))
    if any(output == path or output.is_relative_to(path) for path in protected):
        raise ValueError('isolation report must not overlap source or model inputs')
    args.output = output
    args.output.unlink(missing_ok=True)
    manifest = json.loads((ROOT / 'compiler/frontend/dependencies.json').read_text())
    with tempfile.TemporaryDirectory(prefix='llm2fpga-compile-only-') as directory:
        isolated = Path(directory)
        source_hashes = {}
        for name in manifest['compilation_sources']:
            destination = isolated / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
            source_hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
        for label, source in (('snapshot', args.snapshot), ('package', args.package)):
            destination = isolated / label
            destination.mkdir()
            for name in manifest[f'{label}_files']:
                shutil.copyfile(source / name, destination / name)
        program = '''
import json
from pathlib import Path
import torch
from compiler.frontend.package_inputs import load_compile_inputs
from compiler.frontend.package_model import build_fixed_point_model
inputs = load_compile_inputs(Path('snapshot'), Path('package'),
                             Path('compiler/contracts/tinystories-1m-fixed-point.json'))
model = build_fixed_point_model(inputs)
with torch.no_grad():
    first = model(torch.tensor([[1, 2, 3, 4]], dtype=torch.int64))
    second = model(torch.tensor([[4, 3, 2, 1]], dtype=torch.int64))
assert first.shape == second.shape == (1, 4, 50257)
assert not torch.equal(first, second)
print(json.dumps({'built_without_expected_files': True, 'runtime_inputs_distinct': True}))
'''
        # -E ignores inherited PYTHONPATH, and cwd contains no historical adapters.
        completed = subprocess.run([sys.executable, '-E', '-c', program], cwd=isolated,
                                   text=True, capture_output=True, check=True)
        receipt = json.loads(completed.stdout)
        receipt['compilation_source_hashes'] = source_hashes
        receipt['schema_version'] = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')
    print('isolated model build and distinct same-shape runtime inputs passed')


if __name__ == '__main__':
    main()
