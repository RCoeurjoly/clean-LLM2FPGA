#!/usr/bin/env python3
"""Run one declared Nix build stage, or assemble and verify the final bitstream."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

EXPECTED = '13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00'
FASM_HASH = '7c4196526312282b67bfcb4052a2a256a10f45cbda93e64d90647e0d72af637c'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require_hash(path, expected):
    actual = digest(path)
    if actual != expected:
        raise ValueError(f'SHA-256 mismatch for {path}: expected {expected}, got {actual}')
    return actual


def run_logged(command, output, environment, log_name, timeout=None):
    path = output / log_name
    try:
        with path.open('w') as log:
            subprocess.run(command, cwd=output, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Failed Nix outputs are discarded; expose diagnostics in `nix log` too.
        for diagnostic in (path, output / 'native.log'):
            if diagnostic.is_file():
                with diagnostic.open('rb') as log:
                    log.seek(max(0, diagnostic.stat().st_size - 16384))
                    print(log.read().decode(errors='replace'), file=sys.stderr, flush=True)
        raise


def run_stage(spec, output):
    # Nix supplies the sandbox and dependencies. Never consult the old checkout,
    # the host toolchain, or an archive of prior compilation outputs.
    for name in spec['inputs']:
        if not Path(name).is_file():
            raise FileNotFoundError('Missing generated input: ' + name)
    output.mkdir(parents=True, exist_ok=False)
    if spec.get('model_images'):
        images = sorted(Path(spec['model_images']).glob('input-*.hex'))
        if not images:
            raise FileNotFoundError('Model core produced no memory images')
        for image in images:
            shutil.copyfile(image, output / image.name)
    if spec.get('decompress_input'):
        with gzip.open(spec['decompress_input'], 'rb') as source:
            with (output / 'native-input.json').open('wb') as target:
                shutil.copyfileobj(source, target)
    command = [arg.replace('@OUTPUT@', str(output)) for arg in spec['argv']]
    environment = os.environ | spec.get('environment', {})
    print('Running ' + spec.get('name', command[0]), flush=True)
    run_logged(command, output, environment, 'build.log', spec['timeout_seconds'])
    hashes = {name: digest(output / name) for name in spec['expected_outputs']}
    report = {'argv': command, 'outputs': hashes, 'historical_matches': {
        name: value == spec['expected_outputs'][name] for name, value in hashes.items()}}
    (output / 'build-result.json').write_text(json.dumps(report, indent=2) + '\n')


def assemble(spec, output):
    from tools.bit_header import normalize_file
    from tools.verify_decoded_frames import verify_frames

    require_hash(spec['fasm'], FASM_HASH)
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ | spec['environment']
    db = env['PRJXRAY_DB_DIR']
    part = db + '/xc7k480tffg1156-1/part.yaml'
    run_logged(['bash', spec['script'], spec['fasm'], 'model.bit', 'model.frm', db, part],
               output, env, 'conversion.log')
    timestamp = normalize_file(output / 'model.bit')
    run_logged(['bitread', '--part_file', part, '-o', 'model.bit.decoded-bits', 'model.bit'],
               output, env, 'normalized-decode.log')
    frames = verify_frames(output / 'model.frm', output / 'model.bit.decoded-bits')
    sha = require_hash(output / 'model.bit', EXPECTED)
    (output / 'SHA256SUMS').write_text(f'{sha}  model.bit\n')
    (output / 'result.json').write_text(json.dumps({
        'status': 'matched', 'sha256': sha, 'source_fasm_sha256': FASM_HASH,
        'timestamp_normalization': timestamp, 'frames': frames,
        'hardware_programmed': False}, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'assemble'))
    parser.add_argument('spec', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    {'stage': run_stage, 'assemble': assemble}[args.action](spec, args.output.resolve())


if __name__ == '__main__':
    main()
