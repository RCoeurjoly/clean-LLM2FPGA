#!/usr/bin/env python3
import hashlib
from pathlib import Path
import subprocess
import sys

from tools.bit_header import normalize_file
from tools.verify_decoded_frames import verify_frames

EXPECTED = '13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00'
FASM_HASH = '7c4196526312282b67bfcb4052a2a256a10f45cbda93e64d90647e0d72af637c'


def require_hash(path, expected):
    with Path(path).open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError(f'SHA-256 mismatch for {path}: expected {expected}, got {actual}')


def assemble(fasm, db):
    require_hash(fasm, FASM_HASH)
    part = str(Path(db) / 'xc7k480tffg1156-1/part.yaml')
    subprocess.run(['fasm2frames', '--db-root', db, '--part', 'xc7k480tffg1156-1',
                    fasm, 'model.frm'], check=True)
    subprocess.run(['xc7frames2bit', '--part_file', part, '--frm_file', 'model.frm',
                    '--output_file', 'model.bit'], check=True)
    normalize_file('model.bit')
    subprocess.run(['bitread', '--part_file', part, '-o', 'decoded-bits', 'model.bit'], check=True)
    verify_frames('model.frm', 'decoded-bits')
    require_hash('model.bit', EXPECTED)
    print(f'Bitstream SHA-256 verified: {EXPECTED}')


if __name__ == '__main__':
    assemble(*sys.argv[1:])
