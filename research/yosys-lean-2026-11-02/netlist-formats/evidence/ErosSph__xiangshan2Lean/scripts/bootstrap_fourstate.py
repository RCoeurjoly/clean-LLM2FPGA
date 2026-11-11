"""Install the pinned four-state parser into project-local storage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'verification/fourstate/parser-lock.json'
INSTALL = ROOT / ('.tools/pyslang-12.0.0-cp' + str(sys.version_info.major) + str(sys.version_info.minor))


def load_parser():
    if not (INSTALL / 'pyslang').is_dir():
        raise RuntimeError('Run python scripts/bootstrap_fourstate.py first')
    lock = json.loads(LOCK.read_text())
    receipt = json.loads((INSTALL / 'fourstate-install.json').read_text())
    if receipt['wheel_sha256'] != lock['sha256']:
        raise RuntimeError('Four-state parser install lock mismatch')
    abi = str(sys.version_info.major) + str(sys.version_info.minor)
    if receipt['backend_sha256'] != lock['backend_wheels'].get(abi):
        raise RuntimeError('Four-state backend lock mismatch')
    for name, expected in receipt['files'].items():
        if hashlib.sha256((INSTALL / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Four-state parser file changed: ' + name)
    sys.path.insert(0, str(INSTALL))
    import pyslang
    if pyslang.__version__ != lock['version']:
        raise RuntimeError('Four-state parser version mismatch')
    return pyslang


def main():
    if sys.platform != 'win32' or sys.version_info < (3, 11):
        raise RuntimeError('This parser lock requires Windows x64 and Python >= 3.11')
    lock = json.loads(LOCK.read_text())
    abi = str(sys.version_info.major) + str(sys.version_info.minor)
    if abi not in lock['backend_wheels']:
        raise RuntimeError('Parser backend lock supports CPython 3.11 or 3.13 Windows x64')
    cache = ROOT / '.cache/fourstate'
    cache.mkdir(parents=True, exist_ok=True)
    wheel = cache / lock['wheel']
    if not wheel.exists():
        with urllib.request.urlopen('https://pypi.org/pypi/pyslang/' + lock['version'] + '/json', timeout=30) as response:
            metadata = json.load(response)
        entry = next(f for f in metadata['urls'] if f['filename'] == lock['wheel'])
        if entry['digests']['sha256'] != lock['sha256']:
            raise RuntimeError('PyPI wheel does not match parser lock')
        with urllib.request.urlopen(entry['url'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != lock['sha256']:
            raise RuntimeError('Downloaded parser hash mismatch')
        wheel.write_bytes(data)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != lock['sha256']:
        raise RuntimeError('Cached parser hash mismatch')
    backend_name = f'nanobind_backend-1.0.0-cp{abi}-cp{abi}-win_amd64.whl'
    backend = cache / backend_name
    expected = lock['backend_wheels'][abi]
    if not backend.exists():
        with urllib.request.urlopen('https://pypi.org/pypi/nanobind-backend/1.0.0/json', timeout=30) as response:
            entry = next(f for f in json.load(response)['urls'] if f['filename'] == backend_name)
        with urllib.request.urlopen(entry['url'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('Parser backend download hash mismatch')
        backend.write_bytes(data)
    if hashlib.sha256(backend.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Parser backend cache hash mismatch')
    if not INSTALL.exists():
        INSTALL.mkdir(parents=True)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(INSTALL)
        with zipfile.ZipFile(backend) as archive:
            archive.extractall(INSTALL)
        files = {p.relative_to(INSTALL).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in INSTALL.rglob('*') if p.is_file()}
        (INSTALL / 'fourstate-install.json').write_text(json.dumps(
            {'wheel_sha256': lock['sha256'], 'backend_sha256': expected, 'files': files}, indent=2), encoding='utf-8')
    print('pyslang', load_parser().__version__, INSTALL)


if __name__ == '__main__':
    main()
