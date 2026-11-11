"""Install the pinned project-local C JSON streaming reader for large RTL files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

from common import ROOT

INSTALL = ROOT / '.tools/json-stream'
FILENAME = 'ijson-3.4.0.post0-cp311-cp311-win_amd64.whl'
SHA256 = 'b39dbf87071f23a23c8077eea2ae7cfeeca9ff9ffec722dfc8b5f352e4dd729c'
URL = 'https://files.pythonhosted.org/packages/fa/bf/932740899e572a97f9be0c6cd64ebda557eae7701ac216fc284aba21786d/' + FILENAME


def bootstrap():
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 11):
        raise RuntimeError('The large-JSON wheel lock currently targets CPython 3.11 / Windows x64')
    wheel = ROOT / '.cache' / FILENAME
    wheel.parent.mkdir(exist_ok=True)
    if not wheel.is_file():
        urllib.request.urlretrieve(URL, wheel)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('Streaming JSON wheel SHA-256 mismatch')
    with zipfile.ZipFile(wheel) as archive:
        for entry in archive.infolist():
            if not (INSTALL / entry.filename).resolve().is_relative_to(INSTALL.resolve()):
                raise RuntimeError('Wheel path escapes installation directory')
        archive.extractall(INSTALL)
    files = {p.relative_to(INSTALL).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in INSTALL.rglob('*') if p.is_file() and '__pycache__' not in p.parts
             and p.name != 'chisel2lean-lock.json'}
    (INSTALL / 'chisel2lean-lock.json').write_text(json.dumps({'wheel': FILENAME, 'wheel_sha256': SHA256, 'files': files}, indent=2), encoding='utf-8')
    print('Installed pinned ijson 3.4.0.post0', flush=True)


def reader():
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 11):
        raise RuntimeError('The streaming JSON lock currently requires CPython 3.11 / Windows x64')
    path = INSTALL / 'chisel2lean-lock.json'
    if not path.is_file():
        raise RuntimeError('Large JSON requires python scripts/bootstrap_json_stream.py (CPython 3.11 / Windows x64)')
    lock = json.loads(path.read_text(encoding='utf-8'))
    if lock['wheel_sha256'] != SHA256:
        raise RuntimeError('Streaming JSON reader differs from the pinned wheel')
    for name, expected in lock['files'].items():
        source = (INSTALL / name).resolve()
        if not source.is_relative_to(INSTALL.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Streaming JSON reader runtime hash mismatch: ' + name)
    sys.path.insert(0, str(INSTALL))
    import ijson
    return ijson


if __name__ == '__main__':
    bootstrap()
