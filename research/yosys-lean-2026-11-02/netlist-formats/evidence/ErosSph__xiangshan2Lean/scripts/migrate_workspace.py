"""Stream a complete workspace backup, verify it remotely, then prune saved outputs.

No deletion is allowed before independent archive verification. Every removed
file is hashed again against the archived manifest. Junctions are never walked.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import tarfile
import time

ROOTS = ('.gitignore', '.publication', '.publish', '.tools', 'chisel-env.ps1',
         'verify-chisel.ps1', 'chisel2lean4-upload.zip', 'chisel-example',
         'chisel2lean', 'chisel2lean4', 'firrtl2lean-pilot', 'xiangshan2Lean')
SOURCE_EXTENSIONS = {'.scala', '.lean', '.py', '.sbt', '.sh', '.ps1', '.md'}
DISPOSABLE_NAMES = {'.cache', '.tools', 'dist', 'target', '__pycache__',
                    'test_run_dir', 'obj_dir', '.bloop', '.bsp', '.metals'}
BLOCK = 1024 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True) + '\n').encode()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(BLOCK), b''):
            h.update(chunk)
    return h.hexdigest()


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def reparse(info):
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400) or stat.S_ISLNK(info.st_mode)


def parts_of(relative):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith('/') or any(p in ('.', '..') or ':' in p or '\\' in p for p in parts):
        raise ValueError('Unsafe relative path: ' + relative)
    return parts


def safe_local(base, relative, leaf_link=False):
    base = Path(base).absolute()
    parts = parts_of(relative)
    candidate = base.joinpath(*parts)
    if os.path.commonpath([str(base), str(candidate)]) != str(base):
        raise ValueError('Path escapes workspace')
    # Check every existing ancestor, rather than resolving through a junction.
    for index in range(len(parts) + 1):
        node = base.joinpath(*parts[:index])
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if reparse(info) and not (leaf_link and index == len(parts)):
            raise ValueError('Path crosses a link: ' + str(node))
    return candidate


def retain(relative, size=0, kind='file'):
    parts = parts_of(relative)
    # Preserve primary repositories and their uncommitted working trees.
    git_index = 2 if parts[0] == '.publish' else 1
    if len(parts) > git_index and parts[git_index] == '.git':
        return True
    if relative == 'chisel2lean4-upload.zip':
        return False
    if any(p in DISPOSABLE_NAMES for p in parts):
        return False
    if parts[0] == 'chisel-example' and 'null' in parts[1:]:
        return False
    if 'experiments' in parts or 'generated' in parts:
        return kind == 'file' and size <= BLOCK and PurePosixPath(relative).suffix.lower() in SOURCE_EXTENSIONS
    if PurePosixPath(relative).suffix.lower() in {'.olean', '.ilean', '.pyc', '.pyo', '.class', '.o', '.obj', '.gch'}:
        return False
    return True


def walk(base):
    def visit(path):
        relative = path.relative_to(base).as_posix()
        info = path.lstat()
        if reparse(info):
            yield relative, info, 'link'
        elif stat.S_ISDIR(info.st_mode):
            yield relative, info, 'directory'
            with os.scandir(path) as entries:
                children = sorted((Path(e.path) for e in entries), key=lambda p: p.name)
            for child in children:
                yield from visit(child)
        elif stat.S_ISREG(info.st_mode):
            yield relative, info, 'file'
        else:
            raise ValueError('Unsupported filesystem entry: ' + str(path))
    for name in ROOTS:
        path = base / name
        if path.exists() or path.is_symlink():
            yield from visit(path)


def small_file(base, relative, info):
    path = safe_local(base, relative)
    with path.open('rb') as stream:
        data = stream.read()
        after = os.fstat(stream.fileno())
    if (after.st_size, after.st_mtime_ns, after.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino) or len(data) != info.st_size:
        raise ValueError('Source changed during backup: ' + relative)
    return data, hashlib.sha256(data).hexdigest()


def prefetched(base):
    # Keep order and bound the prefetched payload to at most 64 MiB.
    queue = deque()
    with ThreadPoolExecutor(max_workers=16) as pool:
        for relative, info, kind in walk(base):
            future = pool.submit(small_file, base, relative, info) if kind == 'file' and info.st_size <= BLOCK else None
            queue.append((relative, info, kind, future))
            if len(queue) >= 64:
                yield queue.popleft()
        yield from queue


class HashReader:
    def __init__(self, stream):
        self.stream, self.hash, self.bytes = stream, hashlib.sha256(), 0

    def read(self, count=-1):
        data = self.stream.read(count)
        self.hash.update(data)
        self.bytes += len(data)
        return data


class HashWriter:
    def __init__(self, stream):
        self.stream, self.hash, self.bytes = stream, hashlib.sha256(), 0

    def write(self, data):
        count = self.stream.write(data)
        self.hash.update(memoryview(data)[:count])
        self.bytes += count
        return count

    def flush(self):
        return self.stream.flush()


class SftpWriter:
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(bytes(data))
        return len(data)

    def flush(self):
        self.stream.flush()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()  # Wait for all pipelined SFTP write acknowledgements.


def core(record):
    return {k: record[k] for k in ('path', 'kind', 'bytes', 'sha256', 'target', 'retain') if k in record}


def _pack(source, work, destination, sftp=None, remote_directory=None):
    import zstandard
    source, work, destination = source.resolve(), work.resolve(), destination.absolute()
    if reparse(source.lstat()):
        raise ValueError('Source must be a real directory')
    work.mkdir(parents=True, exist_ok=True)
    if sftp is None:
        destination.mkdir(parents=True, exist_ok=True)
    archive = destination / 'workspace.tar.zst'
    partial = destination / 'workspace.tar.zst.partial'
    existing_output = False
    if sftp is None:
        existing_output = archive.exists() or partial.exists()
    else:
        for name in (archive.name, partial.name, 'packed.json'):
            try:
                sftp.stat(posixpath.join(remote_directory, name))
                existing_output = True
            except FileNotFoundError:
                pass
    if existing_output or any(p.exists() for p in (work/'packed.json', work/'files.jsonl')):
        raise ValueError('Choose a fresh snapshot; preserve existing backup files')
    manifest = work/'files.jsonl'
    counts = {'file': 0, 'directory': 0, 'link': 0}
    total = kept = kept_files = 0
    observed = hashlib.sha256()
    started = time.monotonic()
    progress_at = started
    if sftp is None:
        output = partial.open('xb', buffering=8*BLOCK)
    else:
        output = sftp.open(posixpath.join(remote_directory, partial.name), 'wx', bufsize=256*1024)
        output.set_pipelined(True)
        output = SftpWriter(output)
    with output as raw, manifest.open('xb') as inventory:
        sink = HashWriter(raw)
        compressor = zstandard.ZstdCompressor(level=3, threads=4, write_checksum=True)
        with compressor.stream_writer(sink, closefd=False) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', bufsize=BLOCK,
                              format=tarfile.PAX_FORMAT, copybufsize=BLOCK) as tar:
                for relative, info, kind, future in prefetched(source):
                    path = safe_local(source, relative, leaf_link=kind == 'link') if future is None else source.joinpath(*parts_of(relative))
                    record = {'path': relative, 'kind': kind, 'bytes': info.st_size if kind == 'file' else 0,
                              'mtime_ns': info.st_mtime_ns, 'mode': stat.S_IMODE(info.st_mode)}
                    record['retain'] = retain(relative, record['bytes'], kind)
                    header = tarfile.TarInfo('ICT/' + relative)
                    header.mtime, header.mode = int(info.st_mtime), record['mode']
                    if kind == 'file':
                        header.size = info.st_size
                        if future is not None:
                            import io
                            data, record['sha256'] = future.result()
                            tar.addfile(header, io.BytesIO(data))
                        else:
                            with path.open('rb') as stream:
                                reader = HashReader(stream)
                                tar.addfile(header, reader)
                                after = os.fstat(stream.fileno())
                            if (after.st_size, after.st_mtime_ns, after.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                                raise ValueError('Source changed during backup: ' + relative)
                            if reader.bytes != info.st_size:
                                raise ValueError('Short source read: ' + relative)
                            record['sha256'] = reader.hash.hexdigest()
                        total += info.st_size
                        if record['retain']:
                            kept += info.st_size
                            kept_files += 1
                    elif kind == 'directory':
                        header.type = tarfile.DIRTYPE
                        tar.addfile(header)
                    else:
                        source_target = os.readlink(path)
                        resolved = path.resolve(strict=False)
                        if not resolved.is_relative_to(source):
                            raise ValueError('Link target outside full snapshot: ' + relative)
                        target = posixpath.relpath('ICT/' + resolved.relative_to(source).as_posix(),
                                                  posixpath.dirname('ICT/' + relative))
                        record.update(target=target, source_target=source_target,
                                      reparse_tag=getattr(info, 'st_reparse_tag', None))
                        header.type, header.linkname = tarfile.SYMTYPE, target
                        tar.addfile(header)
                    counts[kind] += 1
                    inventory.write(encoded(record))
                    observed.update(encoded(core(record)))
                    if time.monotonic() - progress_at >= 15:
                        print(json.dumps({'stage': 'packing', 'files': counts['file'], 'source_bytes': total,
                                          'compressed_bytes': sink.bytes, 'elapsed_seconds': round(time.monotonic()-started)}), flush=True)
                        progress_at = time.monotonic()
                inventory.flush()
                snapshot = {'format': 'ict-complete-workspace-v1', 'created_at': now(), 'source': str(source),
                            'roots': list(ROOTS), 'counts': counts, 'source_bytes': total,
                            'retained_files': kept_files, 'retained_bytes': kept,
                            'manifest_sha256': digest(manifest), 'records_sha256': observed.hexdigest(),
                            'note': 'All files, including ignored/untracked outputs and Git history, are backed up. Internal Windows junctions are represented by relative links.'}
                tar.add(manifest, arcname='_MIGRATION/files.jsonl', recursive=False)
                payload = encoded(snapshot)
                import io
                header = tarfile.TarInfo('_MIGRATION/snapshot.json')
                header.size = len(payload)
                tar.addfile(header, io.BytesIO(payload))
        sink.flush()
    if sftp is None:
        partial.rename(archive)
    else:
        sftp.rename(posixpath.join(remote_directory, partial.name), posixpath.join(remote_directory, archive.name))
    report = dict(snapshot, archive_filename=archive.name, archive_sha256=sink.hash.hexdigest(),
                  compressed_bytes=sink.bytes, elapsed_seconds=round(time.monotonic()-started, 1), status='PACKED_NOT_YET_VERIFIED')
    write_new(work/'packed.json', report)
    if sftp is None:
        write_new(destination/'packed.json', report)
    else:
        with sftp.open(posixpath.join(remote_directory, 'packed.json'), 'wx') as stream:
            stream.write(encoded(report))
    print(json.dumps(report, indent=2), flush=True)


def pack(source, work, destination, ssh=None):
    if ssh is None:
        return _pack(source, work, destination)
    import getpass
    import sys
    sys.path.insert(0, str(source/'.migration/python-deps'))
    import paramiko
    client = paramiko.SSHClient()
    client.load_system_host_keys(str(Path.home()/'.ssh/known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(ssh['host'], port=ssh['port'], username=ssh['user'],
                       password=getpass.getpass('SSH password (not stored): '),
                       allow_agent=False, look_for_keys=False, timeout=20, banner_timeout=20, auth_timeout=20)
        sftp = paramiko.SFTPClient.from_transport(client.get_transport(), window_size=16*BLOCK)
        try:
            # This task-owned verifier is uploaded before the completed marker.
            sftp.put(__file__, posixpath.join(ssh['directory'], 'tools/scripts/migrate_workspace.py'))
            return _pack(source, work, destination, sftp, ssh['directory'])
        finally:
            sftp.close()
    finally:
        client.close()


def checked_remove(source, record, apply):
    try:
        path = safe_local(source, record['path'])
        if apply:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_size != record['bytes'] or digest(path) != record['sha256']:
                raise ValueError('Source changed after backup; preserved')
            safe_local(source, record['path'])
            after = path.stat()
            if (after.st_mtime_ns, after.st_size, after.st_ino, after.st_dev) != (before.st_mtime_ns, before.st_size, before.st_ino, before.st_dev):
                raise ValueError('Source changed during deletion check; preserved')
            if getattr(before, 'st_file_attributes', 0) & 1 or not os.access(path, os.W_OK):
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
            path.unlink()
        return record, None
    except (OSError, ValueError) as exc:
        return record, {'path':record['path'],'error':str(exc)}


def cleanup_files(source, records, apply):
    queue = deque()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for record in records:
            if record['kind'] != 'file' or record['retain']:
                continue
            queue.append(pool.submit(checked_remove, source, record, apply))
            if len(queue) >= 64:
                yield queue.popleft().result()
        for future in queue:
            yield future.result()


def verify(directory, extract):
    import zstandard
    directory, extract = directory.resolve(), extract.absolute()
    packed = json.loads((directory/'packed.json').read_text())
    archive = directory/'workspace.tar.zst'
    if packed['archive_filename'] != archive.name or archive.stat().st_size != packed['compressed_bytes']:
        raise ValueError('Archive size/name does not match')
    pending = extract.with_name(extract.name + '.pending')
    if extract.exists() or pending.exists() or (directory/'verified.json').exists():
        raise ValueError('Preserve previous verification/extraction and select fresh paths')
    pending.mkdir(parents=True)
    observations, manifest_data, snapshot = [], None, None
    names, links = set(), []
    total, progress_at = 0, time.monotonic()
    with archive.open('rb', buffering=8*BLOCK) as raw:
        reader = HashReader(raw)
        with zstandard.ZstdDecompressor().stream_reader(reader, closefd=False) as decoded:
            with tarfile.open(fileobj=decoded, mode='r|', bufsize=BLOCK) as tar:
                for item in tar:
                    if item.name in names:
                        raise ValueError('Duplicate archive path: ' + item.name)
                    names.add(item.name)
                    if item.name in ('_MIGRATION/files.jsonl', '_MIGRATION/snapshot.json'):
                        if not item.isfile() or item.size > 128*BLOCK:
                            raise ValueError('Invalid snapshot metadata')
                        value = tar.extractfile(item).read()
                        if item.name.endswith('files.jsonl'):
                            manifest_data = value
                        else:
                            snapshot = json.loads(value)
                        continue
                    if not item.name.startswith('ICT/'):
                        raise ValueError('Unexpected archive root: ' + item.name)
                    relative = item.name[4:]
                    parts_of(relative)
                    kind = 'file' if item.isfile() else 'directory' if item.isdir() else 'link' if item.issym() else None
                    if kind is None:
                        raise ValueError('Unexpected archive entry type')
                    record = {'path': relative, 'kind': kind, 'bytes': item.size if kind == 'file' else 0,
                              'retain': retain(relative, item.size if kind == 'file' else 0, kind)}
                    target = safe_local(pending, 'ICT/' + relative)
                    if kind == 'file':
                        output = None
                        if record['retain']:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            output = target.open('xb')
                        try:
                            data, hashed, count = tar.extractfile(item), hashlib.sha256(), 0
                            for chunk in iter(lambda: data.read(BLOCK), b''):
                                hashed.update(chunk)
                                count += len(chunk)
                                if output is not None:
                                    output.write(chunk)
                        finally:
                            if output is not None:
                                output.close()
                        if count != item.size:
                            raise ValueError('Truncated entry: ' + relative)
                        record['sha256'] = hashed.hexdigest()
                        total += count
                    elif kind == 'link':
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname('ICT/'+relative), item.linkname))
                        if item.linkname.startswith('/') or not resolved.startswith('ICT/') or '\\' in item.linkname or ':' in item.linkname:
                            raise ValueError('Archive link escapes snapshot')
                        record['target'] = item.linkname
                        if record['retain']:
                            links.append((target, item.linkname))
                    elif record['retain']:
                        target.mkdir(parents=True, exist_ok=True)
                    observations.append(record)
                    if time.monotonic()-progress_at >= 15:
                        print(json.dumps({'stage':'verifying','entries':len(observations),'source_bytes':total}), flush=True)
                        progress_at = time.monotonic()
            # Read the complete compressed frame and require its checksum to pass.
            while decoded.read(BLOCK):
                pass
    if reader.hash.hexdigest() != packed['archive_sha256'] or reader.bytes != packed['compressed_bytes']:
        raise ValueError('Compressed archive SHA256 mismatch')
    if manifest_data is None or snapshot is None or snapshot['format'] != 'ict-complete-workspace-v1':
        raise ValueError('Missing snapshot metadata')
    if hashlib.sha256(manifest_data).hexdigest() != packed['manifest_sha256']:
        raise ValueError('Manifest SHA256 mismatch')
    claims = [json.loads(line) for line in manifest_data.splitlines()]
    if observations != [core(record) for record in claims]:
        raise ValueError('Archived file bytes, links, or inventory do not match the complete manifest')
    for key in ('counts', 'source_bytes', 'retained_files', 'retained_bytes', 'records_sha256', 'manifest_sha256', 'source'):
        if snapshot[key] != packed[key]:
            raise ValueError('Snapshot metadata mismatch: ' + key)
    if total != packed['source_bytes']:
        raise ValueError('Total source byte count mismatch')
    for target, link in links:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)
    extract.parent.mkdir(parents=True, exist_ok=True)
    pending.rename(extract)
    report = dict(packed, status='PASS', verified_at=now(), source_directory=str(extract),
                  verification='Full archive SHA256, every file SHA256, inventory, byte counts and internal links checked independently on server.')
    write_new(directory/'verified.json', report)
    print(json.dumps(report, indent=2), flush=True)


def clean(source, work, proof, archive=None, apply=False, presence=None):
    source, work = source.resolve(), work.resolve()
    packed = json.loads((work/'packed.json').read_text())
    verified = json.loads(proof.read_text())
    manifest = work/'files.jsonl'
    if verified.get('status') != 'PASS' or str(source) != packed['source']:
        raise ValueError('Full independent verification and original source are required')
    for key in ('archive_sha256', 'compressed_bytes', 'manifest_sha256', 'counts', 'source_bytes', 'source'):
        if verified.get(key) != packed.get(key):
            raise ValueError('Verification does not belong to this full backup: ' + key)
    if digest(manifest) != packed['manifest_sha256']:
        raise ValueError('Inventory changed')
    if archive is not None:
        if archive.stat().st_size != packed['compressed_bytes']:
            raise ValueError('Remote archive changed')
    else:
        if presence is None:
            raise ValueError('Require a reachable archive or a fresh authenticated server hash check')
        present = json.loads(presence.read_text())
        age = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(present['checked_at'])).total_seconds()
        if present.get('status') != 'PASS' or not 0 <= age < 900 or present.get('archive_sha256') != packed['archive_sha256'] or present.get('compressed_bytes') != packed['compressed_bytes']:
            raise ValueError('Fresh server archive presence check does not match')
    records = [json.loads(line) for line in manifest.read_bytes().splitlines()]
    for record in records:
        if retain(record['path'], record['bytes'], record['kind']) != record['retain']:
            raise ValueError('Retention policy mismatch')
    report = {'status':'PLANNED', 'apply':apply, 'archive_sha256':packed['archive_sha256'],
              'before_free_bytes':shutil.disk_usage(source).free, 'files':0, 'bytes':0,
              'retained_files':0, 'links':0, 'skipped':[], 'started_at':now()}
    output = work/('cleanup.jsonl' if apply else 'cleanup-plan.jsonl')
    if output.exists():
        raise ValueError('Preserve existing cleanup records')
    progress_at = time.monotonic()
    with output.open('x', encoding='utf-8', buffering=1) as events:
        report['retained_files'] = sum(1 for r in records if r['kind']=='file' and r['retain'])
        for record, issue in cleanup_files(source, records, apply):
            if issue is None:
                report['files'] += 1
                report['bytes'] += record['bytes']
                events.write(json.dumps({'path':record['path'],'status':'DELETED' if apply else 'WOULD_DELETE','sha256':record['sha256'],'bytes':record['bytes']})+'\n')
            else:
                report['skipped'].append(issue)
                events.write(json.dumps(dict(issue,status='PRESERVED'))+'\n')
            if time.monotonic()-progress_at >= 15:
                print(json.dumps({'stage':'cleaning' if apply else 'planning','files':report['files'],'bytes':report['bytes'],'skipped':len(report['skipped'])}),flush=True)
                progress_at = time.monotonic()
        if apply:
            for record in records:
                if record['kind'] != 'link' or record['retain']:
                    continue
                path = safe_local(source, record['path'], leaf_link=True)
                try:
                    current = path.lstat()
                    if not reparse(current) or os.readlink(path) != record['source_target']:
                        raise ValueError('Link changed; preserved')
                    if os.name == 'nt' and getattr(current,'st_reparse_tag',None) == 0xA0000003:
                        path.rmdir()  # Removes the junction itself, never its target.
                    else:
                        path.unlink()
                    report['links'] += 1
                except (OSError,ValueError) as exc:
                    report['skipped'].append({'path':record['path'],'error':str(exc)})
            for record in sorted(records,key=lambda r:r['path'].count('/'),reverse=True):
                if record['kind'] == 'directory' and not record['retain']:
                    try:
                        safe_local(source,record['path']).rmdir()  # Empty directories only.
                    except OSError:
                        pass
    report.update(status=('PARTIAL' if report['skipped'] else 'PASS') if apply else 'PLANNED',
                  after_free_bytes=shutil.disk_usage(source).free, finished_at=now())
    report['reclaimed_free_bytes'] = report['after_free_bytes']-report['before_free_bytes']
    write_new(work/('cleanup-result.json' if apply else 'cleanup-plan.json'),report)
    print(json.dumps(report,indent=2),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('pack')
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True)
    p.add_argument('--ssh-host')
    p.add_argument('--ssh-port',type=int,default=22)
    p.add_argument('--ssh-user')
    p.add_argument('--remote-directory')
    p=sub.add_parser('verify')
    p.add_argument('--directory',type=Path,required=True)
    p.add_argument('--extract',type=Path,required=True)
    p=sub.add_parser('clean')
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--proof',type=Path,required=True)
    p.add_argument('--archive',type=Path)
    p.add_argument('--presence',type=Path)
    p.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    values=vars(args).copy()
    values.pop('action')
    if args.action=='pack':
        ssh={name:values.pop('ssh_'+name) for name in ('host','port','user')}
        ssh['directory']=values.pop('remote_directory')
        if ssh['host']:
            if not ssh['user'] or not ssh['directory'] or not ssh['directory'].startswith('/'):
                parser.error('SFTP requires user and absolute remote directory')
            values['ssh']=ssh
    globals()[args.action](**values)


if __name__=='__main__':
    main()
