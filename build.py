#!/usr/bin/env python3
"""Reproduce the recorded image without access to the original workspace."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
R = Path('artifacts/task6/board-fit-2026-10-25')
TRIAL = R / 'probe/full-model-authorized-hardware-trial-v1'
EXPECTED = '13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00'
FASM_HASH = '7c4196526312282b67bfcb4052a2a256a10f45cbda93e64d90647e0d72af637c'

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def isolation(output):
    # No /home bind: neither the old workspace nor user Python packages are visible.
    cmd = ['bwrap', '--die-with-parent', '--unshare-net', '--ro-bind', '/nix', '/nix',
           '--ro-bind', '/usr', '/usr', '--ro-bind', '/bin', '/bin',
           '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
           '--ro-bind', '/etc', '/etc', '--proc', '/proc', '--dev', '/dev',
           '--tmpfs', '/tmp', '--ro-bind', str(ROOT), '/repo',
           '--bind', str(output), '/out', '--chdir', '/out']
    return cmd

def verify():
    files = json.loads((ROOT / 'provenance/files.json').read_text())
    for item in files:
        if digest(ROOT / item['path']) != item['sha256']:
            raise ValueError('Changed snapshot file: ' + item['path'])
    if digest(ROOT / 'reference/expected.bit') != EXPECTED:
        raise ValueError('Wrong reference bitstream')
    print(f'Verified {len(files)} copied files and exact reference bitstream')

def convert(output, fasm):
    if digest(fasm) != FASM_HASH:
        raise ValueError('FASM differs from the original image; refusing an identical-image claim')
    output.mkdir(parents=True, exist_ok=False)
    exp = json.loads((ROOT / 'snapshot' / TRIAL / 'experiment.json').read_text())
    env = exp['environment_overrides']
    # Preserve tool selection; discard the original interactive PATH entries.
    pinned_path = ':'.join(p for p in env['PATH'].split(':') if p.startswith('/nix/store/'))
    env['PATH'] = pinned_path + ':/usr/bin:/bin'
    script = '/repo/snapshot/deliverables/tasks-4-5-6/scripts/pipeline/build_and_validate_xc7_bitstream.sh'
    db = env['PRJXRAY_DB_DIR']
    shutil.copyfile(fasm, output / 'input.fasm')
    command = isolation(output)
    for key, value in env.items():
        command += ['--setenv', key, value]
    command += ['--setenv', 'HOME', '/tmp', 'bash', script, '/out/input.fasm',
                'model.bit', 'model.frm', db, db + '/xc7k480tffg1156-1/part.yaml']
    report = dict(command=command, source_fasm_sha256=digest(fasm), old_workspace_visible=False)
    (output / 'command.json').write_text(json.dumps(report, indent=2) + '\n')
    started = time.monotonic()
    with (output / 'conversion.log').open('w') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    report.update(exit_code=result.returncode, seconds=time.monotonic()-started)
    if result.returncode:
        (output / 'result.json').write_text(json.dumps(report, indent=2)+'\n')
        raise RuntimeError('Conversion failed; see ' + str(output / 'conversion.log'))
    finish(output, report)

def finish(output, report):
    from tools.bit_header import normalize_file
    report['timestamp_normalization'] = normalize_file(output/'model.bit')
    exp = json.loads((ROOT / 'snapshot' / TRIAL / 'experiment.json').read_text())
    db = exp['environment_overrides']['PRJXRAY_DB_DIR']
    sdk = Path(exp['environment_overrides']['PRJXRAY_PYTHON_DIR']).parents[2]
    command = isolation(output) + [str(sdk/'bin/bitread'), '--part_file',
        db+'/xc7k480tffg1156-1/part.yaml', '-o', 'model.bit.decoded-bits', 'model.bit']
    with (output/'normalized-decode.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    spec = importlib.util.spec_from_file_location('frames', ROOT / 'snapshot' / R / 'verify_decoded_frames.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    report['frames'] = module.verify_frames(output/'model.frm', output/'model.bit.decoded-bits')
    report['sha256'] = digest(output/'model.bit')
    report['byte_identical'] = (output/'model.bit').read_bytes() == (ROOT/'reference/expected.bit').read_bytes()
    report['hardware_test_status'] = 'original image failed JTAG reset response; no new programming performed'
    (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    if report['sha256'] != EXPECTED or not report['byte_identical']:
        raise ValueError('Rebuilt bitstream is NOT identical')
    print(json.dumps(report,indent=2))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify','bitstream','finish-bitstream','list-stages'))
    parser.add_argument('--output', type=Path, default=ROOT/'build/bitstream')
    args=parser.parse_args()
    if args.action=='verify': verify()
    elif args.action=='bitstream': convert(args.output.resolve(),ROOT/'reference/route.fasm')
    elif args.action=='finish-bitstream':
        output=args.output.resolve()
        report=json.loads((output/'result.json').read_text())
        if report['exit_code']!=0:raise ValueError('Conversion did not succeed')
        finish(output,report)
    else:
        for p in sorted((ROOT/'snapshot'/R).rglob('spec.json')):
            s=json.loads(p.read_text())
            print(str(p.relative_to(ROOT/'snapshot'))+'\t'+s.get('name',''))
if __name__=='__main__': main()
