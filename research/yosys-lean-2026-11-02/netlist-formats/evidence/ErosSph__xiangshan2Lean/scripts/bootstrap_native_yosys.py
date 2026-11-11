"""Install a hash-locked official Windows/Linux x64 Yosys runtime locally."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request
import argparse
import uuid

from common import ROOT

RELEASE = '2026-10-14'
ARCHIVE = 'oss-cad-suite-windows-x64-20261014.tgz'
ARCHIVE_SHA256 = '580a1bbe938f4af861ff49e42944dc00933d61d087693e43c316023ea07f6e1e'
if sys.platform == 'linux':
    ARCHIVE = 'oss-cad-suite-linux-x64-20261014.tgz'
    ARCHIVE_SHA256 = 'a6ccc6b96b2d985336a1061c78b2fd2bdd73e0f68690da17db88e00457881a11'
URL = f'https://github.com/YosysHQ/oss-cad-suite-build/releases/download/{RELEASE}/{ARCHIVE}'
INSTALL = ROOT / '.tools/oss-cad-suite-20261014'
VERSION = 'Yosys 0.70+16 (git sha1 3bb533450, Release, GNU /usr/bin/x86_64-w64-mingw32-g++ 15.2.1)'


def executable():
    return INSTALL / ('bin/yosys.exe' if sys.platform == 'win32' else 'bin/yosys')


def expected_version(version):
    return version == VERSION if sys.platform == 'win32' else version.startswith('Yosys 0.70+16 (git sha1 3bb533450,')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def environment():
    env = os.environ.copy()
    env['PATH'] = str(INSTALL / 'lib') + os.pathsep + str(INSTALL / 'bin') + os.pathsep + env.get('PATH', '')
    return env


def load_lock(check_all=False):
    path = INSTALL / 'chisel2lean-lock.json'
    if not path.is_file():
        raise RuntimeError('Run python scripts/bootstrap_native_yosys.py first')
    lock = json.loads(path.read_text(encoding='utf-8'))
    if lock.get('archive_sha256') != ARCHIVE_SHA256 or lock.get('release') != RELEASE:
        raise RuntimeError('Native Yosys does not match the pinned official archive')
    if not expected_version(lock.get('yosys_version', '')):
        raise RuntimeError('Native Yosys version is missing or differs; run scripts/bootstrap_native_yosys.py')
    files = {entry['name']: entry['sha256'] for entry in lock['files']}
    binary = executable().relative_to(INSTALL).as_posix()
    if binary not in files:
        raise RuntimeError('Native Yosys executable is absent from its lock')
    critical = [binary] + ([n for n in files if n.startswith('libexec/') and 'yosys' in n] if sys.platform == 'linux' else [])
    for name in files if check_all else critical:
        source = (INSTALL / name).resolve()
        if not source.is_relative_to(INSTALL.resolve()) or digest(source) != files[name]:
            raise RuntimeError('Native Yosys runtime hash mismatch: ' + name)
    return lock


def bootstrap_linux(archive):
    import platform
    if platform.machine() not in {'x86_64', 'AMD64'}:
        raise RuntimeError('The pinned native archive is for Linux x86_64')
    if digest(archive) != ARCHIVE_SHA256:
        raise RuntimeError('Official Linux archive SHA-256 mismatch')
    if INSTALL.exists():
        return load_lock(check_all=True)
    stage = INSTALL.parent / (INSTALL.name + '.extract-' + uuid.uuid4().hex[:8])
    stage.mkdir(parents=True)
    with tarfile.open(archive, 'r:gz') as tar:
        # Python's data filter rejects absolute/escaping links and paths. Keep
        # the official loader, libraries, plugins and their license files.
        for member in tar:
            if not member.name.startswith('oss-cad-suite/') and member.name != 'oss-cad-suite':
                raise RuntimeError('Unexpected official archive root')
            tar.extract(member, stage, filter='data')
    extracted = stage / 'oss-cad-suite'
    if not extracted.resolve().is_relative_to(stage.resolve()):
        raise RuntimeError('Native extraction escaped its staging directory')
    extracted.rename(INSTALL)
    version = subprocess.check_output([str(executable()), '-V'], env=environment(), text=True).strip()
    if not expected_version(version):
        raise RuntimeError('Pinned Linux Yosys revision differs: ' + version)
    selected = [{'name':p.relative_to(INSTALL).as_posix(),'sha256':digest(p)}
                for p in sorted(INSTALL.rglob('*')) if p.is_file()]
    lock = {'release':RELEASE,'archive_url':URL,'archive_sha256':ARCHIVE_SHA256,
            'yosys_version':version,'system':'Linux','files':selected}
    (INSTALL/'chisel2lean-lock.json').write_text(json.dumps(lock,indent=2)+'\n',encoding='utf-8')
    print('Installed ' + version, flush=True)
    return lock


def bootstrap(archive_override=None):
    if sys.platform == 'linux':
        if archive_override is None:
            raise RuntimeError('Supply --archive with the verified official Linux archive')
        return bootstrap_linux(archive_override.resolve())
    if sys.platform != 'win32':
        raise RuntimeError('This native lock targets Windows/Linux x64')
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    archive = cache / ARCHIVE
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        with urllib.request.urlopen(URL, timeout=60) as response, partial.open('wb') as out:
            total = 0
            while chunk := response.read(4 * 1024 * 1024):
                out.write(chunk)
                total += len(chunk)
                if total % (64 * 1024 * 1024) == 0:
                    print(f'Downloaded {total // (1024 * 1024)} MiB', flush=True)
        partial.replace(archive)
    if digest(archive) != ARCHIVE_SHA256:
        raise RuntimeError('Official archive SHA-256 mismatch')
    INSTALL.mkdir(exist_ok=True)
    selected = []
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            parts = Path(member.name).parts
            if not parts or parts[0] != 'oss-cad-suite':
                raise RuntimeError('Unexpected archive root')
            relative = Path(*parts[1:])
            name = relative.as_posix()
            wanted = member.isfile() and (name == 'bin/yosys.exe' or relative.suffix.lower() == '.dll'
                or name.startswith(('share/yosys/', 'lib/yosys/', 'license/', 'licenses/'))
                or name in {'COPYING', 'LICENSE', 'README'})
            if not wanted:
                continue
            target = (INSTALL / relative).resolve()
            if not target.is_relative_to(INSTALL.resolve()):
                raise RuntimeError('Archive path escapes installation directory')
            target.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(member).read()
            expected = hashlib.sha256(data).hexdigest()
            if not target.is_file() or digest(target) != expected:
                target.write_bytes(data)
            selected.append({'name': name, 'sha256': expected})
    version = subprocess.check_output([str(INSTALL / 'bin/yosys.exe'), '-V'], env=environment(), text=True).strip()
    if version != VERSION:
        raise RuntimeError('Official native Yosys version differs from the pinned build: ' + version)
    lock = {'release': RELEASE, 'archive_url': URL, 'archive_sha256': ARCHIVE_SHA256,
            'yosys_version': version, 'files': selected}
    (INSTALL / 'chisel2lean-lock.json').write_text(json.dumps(lock, indent=2), encoding='utf-8')
    print('Installed ' + version, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path)
    bootstrap(parser.parse_args().archive)
