#!/usr/bin/env python3
"""Build the selected TinyStories-1M image and require its original SHA-256."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
EXPECTED = '13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00'
FASM_HASH = '7c4196526312282b67bfcb4052a2a256a10f45cbda93e64d90647e0d72af637c'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def isolation(output):
    # No home-directory mount: the original checkout cannot supply inputs.
    return ['bwrap', '--die-with-parent', '--unshare-net',
            '--ro-bind', '/nix', '/nix', '--ro-bind', '/usr', '/usr',
            '--ro-bind', '/bin', '/bin', '--ro-bind', '/lib', '/lib',
            '--ro-bind', '/lib64', '/lib64', '--ro-bind', '/etc', '/etc',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--ro-bind', str(ROOT), '/repo', '--bind', str(output), '/out',
            '--chdir', '/out']


def verify():
    files = json.loads((ROOT / 'pipeline/files.json').read_text())
    for name, identity in files.items():
        if digest(ROOT / name) != identity:
            raise ValueError('Changed build input: ' + name)
    print(f'Verified {len(files)} build inputs', flush=True)


def convert(output, fasm):
    from tools.bit_header import normalize_file
    from tools.verify_decoded_frames import verify_frames
    if digest(fasm) != FASM_HASH:
        raise ValueError('Generated FASM differs from the original image')
    output.mkdir(parents=True, exist_ok=False)
    env = json.loads((ROOT / 'pipeline/bitstream.json').read_text())
    db = env['PRJXRAY_DB_DIR']
    script = '/repo/snapshot/deliverables/tasks-4-5-6/scripts/pipeline/build_and_validate_xc7_bitstream.sh'
    shutil.copyfile(fasm, output / 'input.fasm')
    command = isolation(output)
    for key, value in env.items():
        command += ['--setenv', key, value]
    command += ['--setenv', 'HOME', '/tmp', 'bash', script, '/out/input.fasm',
                'model.bit', 'model.frm', db, db + '/xc7k480tffg1156-1/part.yaml']
    report = {'command': command, 'source_fasm_sha256': FASM_HASH,
              'status': 'running', 'hardware_programmed': False}
    receipt = output / 'result.json'
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    started = time.monotonic()
    try:
        with (output / 'conversion.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        report['timestamp_normalization'] = normalize_file(output / 'model.bit')
        sdk = Path(env['PRJXRAY_PYTHON_DIR']).parents[2]
        command = isolation(output) + [str(sdk / 'bin/bitread'), '--part_file',
            db + '/xc7k480tffg1156-1/part.yaml', '-o', 'model.bit.decoded-bits', 'model.bit']
        with (output / 'normalized-decode.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        report['frames'] = verify_frames(output / 'model.frm', output / 'model.bit.decoded-bits')
        report['sha256'] = digest(output / 'model.bit')
        if report['sha256'] != EXPECTED:
            raise ValueError('Rebuilt bitstream does not match the original SHA-256')
        report['status'] = 'matched'
    except Exception as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        report['seconds'] = time.monotonic() - started
        receipt.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'all', 'bitstream', 'list-stages'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--fasm', type=Path, help='generated route.fasm for bitstream-only validation')
    args = parser.parse_args()
    if args.action == 'list-stages':
        flow = json.loads((ROOT / 'pipeline/flow.json').read_text())
        print('pytorch-core')
        for stage in flow['stages']:
            print(stage['name'])
        print('bitstream')
    elif args.action == 'bitstream':
        if args.fasm is None:
            parser.error('bitstream requires --fasm; use all to compile from PyTorch')
        convert((args.output or ROOT / 'build/bitstream').resolve(), args.fasm.resolve())
    else:
        from tools.full_build import preflight, run
        verify()
        if args.action == 'verify':
            preflight(json.loads((ROOT / 'pipeline/flow.json').read_text()))
        else:
            run((args.output or ROOT / 'build/full').resolve())


if __name__ == '__main__':
    main()
