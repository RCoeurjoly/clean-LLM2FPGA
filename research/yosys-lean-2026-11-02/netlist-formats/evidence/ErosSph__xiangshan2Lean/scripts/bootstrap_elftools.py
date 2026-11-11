"""Use the pinned pure-Python pyelftools wheel to parse executable images."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import urllib.request

from common import ROOT

URL = 'https://files.pythonhosted.org/packages/af/43/700932c4f0638c3421177144a2e86448c0d75dbaee2c7936bda3f9fd0878/pyelftools-0.32-py3-none-any.whl'
SHA256 = '013df952a006db5e138b1edf6d8a68ecc50630adbd0d83a2d41e7f846163d738'
WHEEL = ROOT / '.tools/downloads/pyelftools-0.32-py3-none-any.whl'


def install():
    if not WHEEL.is_file():
        WHEEL.parent.mkdir(parents=True, exist_ok=True)
        temporary = WHEEL.with_suffix('.part')
        with urllib.request.urlopen(URL, timeout=60) as source, temporary.open('wb') as target:
            while data := source.read(1024*1024):
                target.write(data)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
            raise ValueError('Official pyelftools wheel hash mismatch')
        temporary.replace(WHEEL)
    if hashlib.sha256(WHEEL.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Pinned pyelftools wheel changed')
    return WHEEL


def reader():
    if not WHEEL.is_file():
        raise RuntimeError('Run python scripts/bootstrap_elftools.py on the server first')
    install()
    if str(WHEEL) not in sys.path:
        sys.path.insert(0, str(WHEEL))
    from elftools.elf.elffile import ELFFile
    return ELFFile


if __name__ == '__main__':
    print(install())
