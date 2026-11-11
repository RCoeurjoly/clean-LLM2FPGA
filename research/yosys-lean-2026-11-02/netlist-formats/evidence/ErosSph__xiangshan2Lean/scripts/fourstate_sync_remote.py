"""Python 3.8 stdlib SSH receiver. Every write is anchored under WORKSPACE.

Normally sent on stdin, with the shared validation module injected in memory.
No shell commands, installation, cleanup, or hardware execution are provided.
"""
import io
import os
import stat
import sys
import time
from contextlib import contextmanager
from fourstate_sync_common import (WORKSPACE, SOURCE_LIMIT, EXPORT_LIMIT, ARCHIVE_OVERHEAD,
    identifier, relative, encoded, loads, sha, checksum, inspect_zip, make_zip, check_workspace_space)


def stamp(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class Guard:
    def __init__(self):
        for name in ('O_PATH', 'O_NOFOLLOW', 'O_NOATIME', 'O_DIRECTORY'):
            if not hasattr(os, name):
                raise RuntimeError('required safety flag unavailable: ' + name)
        fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in WORKSPACE.strip('/').split('/'):
                nxt = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            self.root = os.open('.', self.flags, dir_fd=fd)
        finally:
            os.close(fd)
        info = os.fstat(self.root)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(self.root)
            raise ValueError('workspace must be owned by login user and private (0700)')
        self.device, self.inode = info.st_dev, info.st_ino

    @property
    def flags(self):
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME

    def close(self):
        os.close(self.root)

    def validate_dir(self, fd):
        info = os.fstat(fd)
        if info.st_dev != self.device or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('directory device/ownership/permissions changed')

    @contextmanager
    def directory(self, name='', create=False, exclusive=False):
        parts = relative(name) if name else []
        fd = os.dup(self.root)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts)-1
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        if last and exclusive:
                            raise
                nxt = os.open(part, self.flags, dir_fd=fd)
                os.close(fd)
                fd = nxt
                self.validate_dir(fd)
            yield fd
        finally:
            os.close(fd)

    def exists(self, name):
        parts = relative(name)
        try:
            with self.directory('/'.join(parts[:-1])) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or info.st_dev != self.device:
                    raise ValueError('symlink/cross-device target refused: ' + name)
                return True
        except FileNotFoundError:
            return False

    def read(self, name, limit):
        parts = relative(name)
        with self.directory('/'.join(parts[:-1])) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_dev != self.device or
                    before.st_uid != os.geteuid() or before.st_nlink != 1):
                raise ValueError('owned same-device regular file required')
            if limit is not None and before.st_size > limit:
                raise ValueError('file byte budget exceeded: ' + name)
            data = bytearray()
            while True:
                chunk = os.read(fd, 1024**2 if limit is None else min(1024**2, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if limit is not None and len(data) > limit:
                    raise ValueError('file grew beyond budget')
            if stamp(os.fstat(fd)) != stamp(before) or len(data) != before.st_size:
                raise ValueError('file changed during read')
            return bytes(data)
        finally:
            os.close(fd)

    def new_file(self, name, data):
        parts = relative(name)
        with self.directory('/'.join(parts[:-1]), create=True) as parent:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o400)
            os.fsync(parent)

    def listing(self, name=''):
        with self.directory(name) as fd:
            with os.scandir(fd) as entries:
                rows = []
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    rows.append(dict(name=entry.name, mode=info.st_mode, bytes=info.st_size,
                                     inode=info.st_ino, device=info.st_dev))
            return sorted(rows, key=lambda r:r['name'])


def status(guard):
    entries, probes = guard.listing(), {}
    for row in entries:
        name = row['name']
        if name.startswith(('scp-test-', 'sync-probe-')) and name.endswith('.txt'):
            probes[name] = sha(guard.read(name, 4096))
    return dict(root=WORKSPACE, device=guard.device, inode=guard.inode, probes=probes,
                entries=entries, interpreter=sys.version)


def exact_tree(guard, base, paths):
    found, expected_dirs = set(), set()
    for name in paths:
        p = relative(name)
        expected_dirs.update('/'.join(p[:i]) for i in range(1, len(p)))
    def walk(name=''):
        for row in guard.listing(base + ('/'+name if name else '')):
            child = name+'/'+row['name'] if name else row['name']
            if stat.S_ISDIR(row['mode']):
                if child not in expected_dirs:
                    raise ValueError('unexpected release directory')
                walk(child)
            elif stat.S_ISREG(row['mode']):
                found.add(child)
            else:
                raise ValueError('release has nonregular entry')
    walk()
    if found != set(paths):
        raise ValueError('release file set changed')


def verify_release(guard, snapshot, expected_manifest=None):
    base = 'releases/'+identifier(snapshot)
    complete = loads(guard.read(base+'/COMPLETE', 1024**2))
    raw = guard.read(base+'/manifest.json', 1024**2)
    if complete['manifest_sha256'] != sha(raw) or (expected_manifest and sha(raw) != expected_manifest):
        raise ValueError('release manifest mismatch')
    from fourstate_sync_common import records
    manifest = loads(raw)
    rows = records(manifest, 'source')
    if manifest['snapshot_id'] != snapshot:
        raise ValueError('release identity mismatch')
    exact_tree(guard, base+'/src', [r['path'] for r in rows])
    for row in rows:
        content = guard.read(base+'/src/'+row['path'], row['bytes'])
        if len(content) != row['bytes'] or sha(content) != row['sha256']:
            raise ValueError('deployed source changed: '+row['path'])
    receipt = guard.read(base+'/receipt.json', 1024**2)
    if complete['receipt_sha256'] != sha(receipt):
        raise ValueError('release receipt changed')
    return dict(status='VERIFIED', snapshot_id=snapshot, files=len(rows), bytes=manifest['bytes'],
                manifest_sha256=sha(raw), receipt=loads(receipt))


def prepare(guard, req):
    snapshot, transfer = identifier(req['snapshot_id']), identifier(req['transfer_id'])
    checksum(req['archive_sha256']); checksum(req['manifest_sha256'])
    if guard.exists('releases/'+snapshot):
        result = verify_release(guard, snapshot, req['manifest_sha256'])
        result['status'] = 'ALREADY_DEPLOYED'
        return result
    space = check_workspace_space(guard, req.get('archive_bytes',SOURCE_LIMIT+ARCHIVE_OVERHEAD)+req.get('source_bytes',SOURCE_LIMIT+ARCHIVE_OVERHEAD))
    base = 'incoming/'+transfer
    with guard.directory(base, create=True, exclusive=True):
        pass
    ticket = dict(snapshot_id=snapshot, archive_sha256=req['archive_sha256'],
                  manifest_sha256=req['manifest_sha256'], paths=req['paths'],
                  workspace_device=guard.device, workspace_inode=guard.inode)
    if req.get('unrestricted'):ticket['unrestricted']=True
    guard.new_file(base+'/request.json', encoded(ticket))
    return dict(status='READY', transfer_id=transfer, upload_path=WORKSPACE+'/'+base+'/bundle.zip',
                workspace_device=guard.device, workspace_inode=guard.inode, space=space)


def receive(guard, req):
    transfer = identifier(req['transfer_id'])
    base = 'incoming/'+transfer
    ticket = loads(guard.read(base+'/request.json', 1024**2))
    if (ticket['workspace_device'], ticket['workspace_inode']) != (guard.device, guard.inode):
        raise ValueError('workspace identity changed during upload')
    snapshot = identifier(ticket['snapshot_id'])
    data = guard.read(base+'/bundle.zip', None if ticket.get('unrestricted') else SOURCE_LIMIT+ARCHIVE_OVERHEAD)
    if sha(data) != ticket['archive_sha256']:
        raise ValueError('archive SHA256 mismatch')
    manifest, raw, payloads = inspect_zip(data, 'source', ticket['manifest_sha256'], ticket['paths'])
    if manifest['snapshot_id'] != snapshot:
        raise ValueError('snapshot differs from ticket')
    release = 'releases/'+snapshot
    with guard.directory(release, create=True, exclusive=True):
        pass
    with guard.directory(release+'/src', create=True, exclusive=True):
        pass
    for name, content in payloads.items():
        guard.new_file(release+'/src/'+name, content)
    guard.new_file(release+'/manifest.json', raw)
    receipt = dict(status='RECEIVED', snapshot_id=snapshot, transfer_id=transfer,
                   archive_sha256=sha(data), manifest_sha256=sha(raw), files=len(payloads),
                   bytes=manifest['bytes'], root=WORKSPACE, received_unix=int(time.time()))
    guard.new_file(release+'/receipt.json', encoded(receipt))
    # Verify actual extracted bytes, not merely the in-memory payload.
    exact_tree(guard, release+'/src', list(payloads))
    for name, content in payloads.items():
        if guard.read(release+'/src/'+name, len(content)) != content:
            raise ValueError('written source differs')
    guard.new_file(release+'/COMPLETE', encoded(dict(manifest_sha256=sha(raw), receipt_sha256=sha(encoded(receipt)))))
    return verify_release(guard, snapshot, sha(raw))


def export_result(guard, req):
    run, export, snapshot = identifier(req['run_id']), identifier(req['export_id']), identifier(req['snapshot_id'])
    verified = verify_release(guard, snapshot)
    paths = req['files']
    if not isinstance(paths, list) or not paths or len(paths) > 5000:
        raise ValueError('explicit nonempty result whitelist required')
    rows, payloads, oversized, total, seen = [], {}, [], 0, set()
    # Stat all selected files first: a rejected export creates no export directory.
    for path in paths:
        parts = relative(path)
        if parts[0] not in ('reports', 'logs') or path.casefold() in seen:
            raise ValueError('invalid/duplicate result selection')
        seen.add(path.casefold())
        parent = 'runs/'+run+'/'+ '/'.join(parts[:-1])
        with guard.directory(parent) as fd:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != guard.device:
                raise ValueError('result must be same-device regular file')
            total += info.st_size
            oversized.append(dict(path=path, bytes=info.st_size))
    if not req.get('unrestricted') and total > EXPORT_LIMIT:
        raise ValueError('100 MiB export limit exceeded: '+encoded(sorted(oversized,key=lambda r:r['bytes'],reverse=True)).decode())
    actual_total = 0
    for path in paths:
        data = guard.read('runs/'+run+'/'+path, None if req.get('unrestricted') else EXPORT_LIMIT)
        actual_total += len(data)
        if not req.get('unrestricted') and actual_total > EXPORT_LIMIT:
            raise ValueError('results grew beyond 100 MiB during export')
        payloads[path] = data
        rows.append(dict(path=path, bytes=len(data), sha256=sha(data)))
    retained = []
    for row in req.get('retained_assets', []):
        relative(row['path']); checksum(row['sha256'])
        # These hashes come from task receipts, not an implicit multi-GB rehash.
        parts = relative(row['path'])
        with guard.directory('runs/'+run+'/'+ '/'.join(parts[:-1])) as fd:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != guard.device or info.st_size != row['bytes']:
                raise ValueError('retained asset metadata differs')
        retained.append(dict(path=row['path'], bytes=row['bytes'], sha256=row['sha256'],
                             hash_verification='provided_by_task_not_recomputed'))
    manifest = dict(format='fourstate-sync-export-v1', run_id=run, export_id=export, snapshot_id=snapshot,
                    source_manifest_sha256=verified['manifest_sha256'], files=rows,
                    bytes=sum(r['bytes'] for r in rows), retained_assets=retained,
                    scope='Selected reports/logs only; not a complete offline reproduction bundle.')
    if req.get('unrestricted'):manifest['unrestricted']=True
    data = make_zip(manifest, payloads, 'export')
    inspect_zip(data, 'export', sha(encoded(manifest)))
    base = 'runs/'+run+'/exports/'+export
    with guard.directory(base, create=True, exclusive=True):
        pass
    guard.new_file(base+'/bundle.zip', data)
    receipt = dict(status='EXPORTED', run_id=run, export_id=export, snapshot_id=snapshot,
                   archive_sha256=sha(data), manifest_sha256=sha(encoded(manifest)),
                   archive_bytes=len(data), uncompressed_bytes=manifest['bytes'],
                   download_path=WORKSPACE+'/'+base+'/bundle.zip')
    if req.get('unrestricted'):receipt['unrestricted']=True
    guard.new_file(base+'/receipt.json', encoded(receipt))
    guard.new_file(base+'/COMPLETE', encoded(dict(receipt_sha256=sha(encoded(receipt)))))
    return receipt


def export_info(guard, req):
    base = 'runs/'+identifier(req['run_id'])+'/exports/'+identifier(req['export_id'])
    marker = loads(guard.read(base+'/COMPLETE', 1024**2))
    raw = guard.read(base+'/receipt.json', 1024**2)
    if sha(raw) != marker['receipt_sha256']:
        raise ValueError('export receipt changed')
    receipt = loads(raw)
    expected = WORKSPACE+'/'+base+'/bundle.zip'
    if receipt['download_path'] != expected:
        raise ValueError('download path is not the guarded export path')
    data = guard.read(base+'/bundle.zip', None if receipt.get('unrestricted') else EXPORT_LIMIT+ARCHIVE_OVERHEAD)
    if sha(data) != receipt['archive_sha256']:
        raise ValueError('export archive changed')
    return receipt


def smoke(guard, req):
    """Create only an explicitly requested, new sync acceptance fixture."""
    snapshot, run = identifier(req['snapshot_id']), identifier(req['run_id'])
    verified = verify_release(guard, snapshot)
    with guard.directory('runs/'+run, create=True, exclusive=True):
        pass
    report = dict(format='fourstate-sync-smoke-v1', status='SYNC_FIXTURE_ONLY',
                  snapshot_id=snapshot, source_manifest_sha256=verified['manifest_sha256'],
                  hardware_compilation=False, f31_resumed=False)
    guard.new_file('runs/'+run+'/reports/sync-smoke.json', encoded(report))
    # A real Linux no-follow check; the symlink itself stays inside the new run.
    with guard.directory('runs/'+run) as fd:
        os.symlink('reports', 'nofollow-fixture', dir_fd=fd)
    try:
        with guard.directory('runs/'+run+'/nofollow-fixture'):
            pass
    except OSError:
        nofollow = True
    else:
        raise AssertionError('symlink traversal unexpectedly accepted')
    return dict(status='SMOKE_CREATED', run_id=run, nofollow_rejected=nofollow,
                file='reports/sync-smoke.json', sha256=sha(encoded(report)))


def main(request):
    guard = Guard()
    try:
        operations = {'status': lambda g,r:status(g), 'prepare':prepare, 'receive':receive,
                      'verify':lambda g,r:verify_release(g,identifier(r['snapshot_id'])),
                      'export':export_result, 'export-info':export_info, 'smoke':smoke}
        result = operations[request['operation']](guard, request)
        print(encoded(result).decode(), end='')
    finally:
        guard.close()
