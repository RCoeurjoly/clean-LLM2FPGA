"""Portable, stdlib-only validation shared by the local and Python 3.8 receiver."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile

WORKSPACE = '/data2/home/lvhongqing/cgt/chisel2lean-fourstate'
SOURCE_LIMIT = 8 * 1024 ** 2
EXPORT_LIMIT = 100 * 1024 ** 2
ARCHIVE_OVERHEAD = 2 * 1024 ** 2
# Uncompressed input/evidence archive policy, separate from source/export ZIPs.
INPUT_ARCHIVE_LIMIT = 128 * 1024 ** 2
WORKSPACE_FREE_RESERVE = 1024 ** 3
BLOCK = 1024 * 1024
FORBIDDEN = {'.git', '.tools', '.cache', 'target', '.lake', 'generated', 'runs', 'experiments', '__pycache__'}
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM'+str(i) for i in range(1, 10)} | {'LPT'+str(i) for i in range(1, 10)}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
        raise ValueError('invalid identifier')
    return value


def relative(value):
    if not isinstance(value, str) or len(value) > 1024 or not value:
        raise ValueError('nonempty relative path required')
    parts = value.split('/')
    if any(not p or p in ('.', '..') or len(p) > 240 or p.endswith(('.', ' ')) or
           p.split('.')[0].upper() in RESERVED or
           any(ord(c) < 32 or ord(c) == 127 or c in '\\:<>"|?*' for c in p) for p in parts):
        raise ValueError('unsafe relative path: ' + repr(value))
    return parts


def source_path(value):
    parts = relative(value)
    if any(p.casefold() in FORBIDDEN for p in parts):
        raise ValueError('excluded source path: ' + value)
    if Path(value).suffix.lower() in {'.exe', '.dll', '.so', '.o', '.a', '.olean', '.ilean', '.zip', '.gz', '.pyc'}:
        raise ValueError('excluded source suffix: ' + value)
    return parts


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')


def loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def check_workspace_space(guard, required_bytes):
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError('invalid required workspace bytes')
    disk = os.fstatvfs(guard.root)
    free = disk.f_bavail * disk.f_frsize
    if free < required_bytes + WORKSPACE_FREE_RESERVE:
        raise ValueError('insufficient workspace space: need payload plus 1 GiB reserve')
    return dict(free_bytes=free, required_bytes=required_bytes,
                reserve_bytes=WORKSPACE_FREE_RESERVE)


def checksum(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid SHA256')
    return value


def records(manifest, kind):
    expected = 'fourstate-sync-source-v1' if kind == 'source' else 'fourstate-sync-export-v1'
    if manifest.get('format') != expected:
        raise ValueError('unsupported manifest format')
    if kind == 'source':
        identifier(manifest['snapshot_id'])
    else:
        identifier(manifest['run_id'])
        identifier(manifest['export_id'])
        identifier(manifest['snapshot_id'])
    rows = manifest['files']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('invalid file count')
    seen, total = set(), 0
    for row in rows:
        path = row['path']
        parts = source_path(path) if kind == 'source' else relative(path)
        if kind == 'export' and parts[0] not in ('reports', 'logs'):
            raise ValueError('exports may contain only explicit reports/logs paths')
        folded = path.casefold()
        if folded in seen:
            raise ValueError('duplicate/case-conflicting path: ' + path)
        seen.add(folded)
        if type(row['bytes']) is not int or row['bytes'] < 0:
            raise ValueError('invalid byte count')
        checksum(row['sha256'])
        total += row['bytes']
    # A file may not also be an ancestor of another file.
    for name in seen:
        parts = name.split('/')
        if any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ValueError('file/directory collision')
    limit = SOURCE_LIMIT if kind == 'source' else EXPORT_LIMIT
    if (not manifest.get('unrestricted') and total > limit) or manifest.get('bytes') != total:
        raise ValueError('uncompressed byte budget/count mismatch: ' + str(total))
    return rows


def make_zip(manifest, payloads, kind):
    rows = records(manifest, kind)
    prefix = 'src/' if kind == 'source' else 'files/'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        values = [('manifest.json', encoded(manifest))] + [(prefix+r['path'], payloads[r['path']]) for r in rows]
        for name, data in values:
            if name != 'manifest.json':
                row = next(r for r in rows if prefix+r['path'] == name)
                if len(data) != row['bytes'] or sha(data) != row['sha256']:
                    raise ValueError('payload differs from manifest')
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o400) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return stream.getvalue()


def inspect_zip(data, kind, manifest_sha=None, allowed_paths=None):
    limit = SOURCE_LIMIT if kind == 'source' else EXPORT_LIMIT
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not 2 <= len(infos) <= 5001:
            raise ValueError('invalid archive entry count')
        names = set()
        for info in infos:
            relative(info.filename)
            folded = info.filename.casefold()
            if folded in names:
                raise ValueError('duplicate/case-conflicting archive entry')
            names.add(folded)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) != stat.S_IFREG or info.is_dir() or info.flag_bits & 1 or info.extra:
                raise ValueError('regular unencrypted archive file required')
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError('unsupported archive compression')
        mi = archive.getinfo('manifest.json')
        if mi.file_size > 1024**2:
            raise ValueError('manifest too large')
        raw = archive.read(mi)
        if manifest_sha is not None and sha(raw) != checksum(manifest_sha):
            raise ValueError('manifest SHA256 mismatch')
        manifest = loads(raw)
        if not manifest.get('unrestricted') and len(data) > limit + ARCHIVE_OVERHEAD:
            raise ValueError('compressed byte budget exceeded')
        rows = records(manifest, kind)
        prefix = 'src/' if kind == 'source' else 'files/'
        expected = {'manifest.json'} | {prefix+r['path'] for r in rows}
        if {i.filename for i in infos} != expected:
            raise ValueError('archive is not the exact manifest file set')
        if allowed_paths is not None and {r['path'] for r in rows} != set(allowed_paths):
            raise ValueError('archive differs from frozen whitelist')
        result = {}
        for row in rows:
            info = archive.getinfo(prefix+row['path'])
            if info.file_size != row['bytes']:
                raise ValueError('ZIP declared size mismatch')
            content = archive.read(info)
            if len(content) != row['bytes'] or sha(content) != row['sha256']:
                raise ValueError('file SHA256 mismatch: ' + row['path'])
            result[row['path']] = content
    return manifest, raw, result
