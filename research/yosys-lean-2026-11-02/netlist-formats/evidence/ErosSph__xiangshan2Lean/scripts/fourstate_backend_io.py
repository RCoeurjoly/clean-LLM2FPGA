"""Opt-in private storage adapter around the unchanged public backend.

JSON byte streams are archived losslessly after the native producer finishes.
Checkpoint validation, preflight, lowering, FunctionalIR import and Lean checks
are performed by the original modules. The artifact directory is the canonical
model; a reference receipt replaces the final duplicate tree copy. Archived
public checkpoints require this adapter or materialization of their JSON files.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

BasePath = type(Path())
ARCHIVES = {}
CONTEXT = {}
COPY = shutil.copy2
JSON_NAMES = {'before.json', 'unconstrained.json', 'named.json', 'lowered.json', 'exposed.json'}


def key(path):
    return os.path.abspath(os.fspath(path))


class ArchivePath(BasePath):
    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        row = ARCHIVES.get(key(self))
        if row:
            if mode not in ('r', 'rb', 'rt'):
                raise ValueError('Archived artifacts are immutable')
            return gzip.open(row['archive'], 'rb' if 'b' in mode else 'rt',
                             **({} if 'b' in mode else dict(encoding=encoding or 'utf-8', errors=errors, newline=newline)))
        return super().open(mode, buffering, encoding, errors, newline)

    def stat(self, *, follow_symlinks=True):
        row = ARCHIVES.get(key(self))
        if row:
            value = list(os.stat(row['archive'], follow_symlinks=False))
            value[6] = row['bytes']
            return os.stat_result(value)
        return super().stat(follow_symlinks=follow_symlinks)


def checked(path):
    """No symlink, hardlink, cross-device or out-of-run file access."""
    path = Path(key(path))
    root = CONTEXT['run']
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_dev != CONTEXT['device'] or info.st_uid != os.geteuid():
            raise ValueError('Unsafe private artifact path: ' + str(current))
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Single-link regular artifact required')
    return path, info


def fingerprint(path, compressed=False):
    value = hashlib.sha256()
    count = 0
    opener = gzip.open if compressed else open
    with opener(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
            count += len(block)
    return count, value.hexdigest()


def reuse_archive_index(path):
    """Register sealed old JSON storage; public checkpoint code validates bytes.

    Only the archive is read. The original checkpoint and its producer evidence
    remain untouched, and reuse_checkpoint still checks its exact logical SHA.
    """
    path, _ = checked(path)
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('format') != 'fourstate-private-storage-v1':
        raise ValueError('Unknown checkpoint storage index')
    rows = []
    for name, row in report['archives'].items():
        logical = Path(key(name))
        if logical.name not in JSON_NAMES:
            continue
        logical.relative_to(CONTEXT['run'] / 'reports')
        packed, info = checked(row['archive'])
        if packed != logical.with_name(logical.name + '.gz'):
            raise ValueError('Checkpoint archive must be beside its logical name')
        if logical.exists() or key(logical) in ARCHIVES:
            raise ValueError('Checkpoint logical artifact already exists')
        if (info.st_size, fingerprint(packed)[1]) != (row['archive_bytes'], row['archive_sha256']):
            raise ValueError('Checkpoint archive differs from storage receipt')
        ARCHIVES[key(logical)] = dict(row)
        rows.append(dict(logical=str(logical), archive=str(packed), bytes=row['bytes'],
                         sha256=row['sha256'], archive_sha256=row['archive_sha256']))
    if not rows:
        raise ValueError('No checkpoint JSON archive in storage index')
    (CONTEXT['out'] / 'reused-archive-storage.json').write_text(json.dumps(dict(
        index=str(path), index_sha256=fingerprint(path)[1], files=rows,
        logical_validation='Unchanged public reuse_checkpoint verifies every required companion SHA256'), indent=2))
    receipt()


def receipt():
    destination = CONTEXT['out'] / 'artifact-storage.json'
    destination.write_text(json.dumps(dict(format='fourstate-private-storage-v1',
        archives=ARCHIVES, copies_avoided=CONTEXT['copies_avoided'],
        note='Logical JSON hashes refer to decompressed original bytes. Restore with gzip -dc into the logical name before using a public checkpoint outside this adapter.'), indent=2))


def archive(source, target=None):
    source, before = checked(source)
    logical = Path(key(target or source))
    logical.relative_to(CONTEXT['out'])
    packed = logical.with_name(logical.name + '.gz')
    if packed.exists():
        raise ValueError('Archive destination already exists')
    value = hashlib.sha256()
    count = 0
    start = time.monotonic()
    print(json.dumps(dict(event='archive_begin', source=str(source), target=str(packed), bytes=before.st_size)), flush=True)
    with source.open('rb') as src, packed.open('xb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=1, mtime=0) as dst:
            for block in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(block)
                value.update(block)
                count += len(block)
        raw.flush()
        os.fsync(raw.fileno())
    _, after = checked(source)
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Source changed during archive')
    check_count, check_hash = fingerprint(packed, compressed=True)
    if (check_count, check_hash) != (count, value.hexdigest()) or count != before.st_size:
        raise ValueError('Lossless archive verification failed')
    ARCHIVES[str(logical)] = dict(archive=str(packed), bytes=count, sha256=check_hash,
        archive_bytes=packed.stat().st_size, archive_sha256=fingerprint(packed)[1],
        seconds=time.monotonic()-start, source=str(source))
    receipt()
    if logical == source:
        # Only the newly generated file is removed, after byte-exact verification.
        source.relative_to(CONTEXT['out'])
        source.unlink()
    print(json.dumps(dict(event='archive_complete', logical=str(logical), **ARCHIVES[str(logical)])), flush=True)
    return str(logical)


def copy2(source, target, *args, **kwargs):
    source_key, target_key = key(source), key(target)
    model = CONTEXT['model']
    try:
        Path(target_key).relative_to(model)
        Path(source_key).relative_to(CONTEXT['out'])
        CONTEXT['copies_avoided'].append(dict(source=source_key, requested=target_key))
        return str(target)
    except ValueError:
        pass
    if Path(target_key).name in JSON_NAMES:
        if source_key in ARCHIVES:
            row = ARCHIVES[source_key]
            packed = target_key + '.gz'
            checked(row['archive'])
            COPY(row['archive'], packed, *args, **kwargs)
            if fingerprint(packed) != fingerprint(row['archive']):
                raise ValueError('Archived copy differs')
            ARCHIVES[target_key] = dict(row, archive=packed, source=source_key)
            receipt()
            return str(target)
        return archive(source_key, target_key)
    return COPY(source, target, *args, **kwargs)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--await-label')
    parser.add_argument('--reuse-archive-index', type=Path)
    parser.add_argument('--stream-json', action='store_true')
    parser.add_argument('--stream-output-json', action='store_true')
    parser.add_argument('--cache-artifacts', action='store_true')
    parser.add_argument('--release-cache-file', type=Path, action='append', default=[])
    parser.add_argument('backend_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    allowed = Path('/data2/home/lvhongqing/cgt/chisel2lean-fourstate')
    run.relative_to(allowed)
    out = args.out.resolve(strict=True)
    out.relative_to(run / 'reports')
    project = args.project.resolve(strict=True)
    project.relative_to(run / 'work')
    CONTEXT.update(run=run, out=out, model=out/'model', device=run.stat().st_dev, copies_avoided=[])
    if args.stream_output_json and not args.stream_json:
        raise ValueError('Native JSON output streaming requires the verified private input reader')
    cache=None
    if args.release_cache_file and not args.cache_artifacts:
        raise ValueError('Cache release files require explicit private cache mode')
    if args.cache_artifacts:
        from fourstate_backend_cache import advise, ArtifactCache
        released=[advise(path,run) for path in args.release_cache_file]
        probe=out/'cache-advice-probe.bin';probe.write_bytes(b'cache IO selfcheck\n'*4096)
        before=fingerprint(probe);identity=probe.stat()
        advice=advise(probe,run)
        if fingerprint(probe)!=before or (probe.stat().st_ino,probe.stat().st_mtime_ns,probe.stat().st_ctime_ns)!=(identity.st_ino,identity.st_mtime_ns,identity.st_ctime_ns):
            raise ValueError('Cache advice changed private file bytes or identity')
        (out/'cache-selfcheck.json').write_text(json.dumps(dict(status='PRIVATE_CACHE_ADVICE_BYTES_UNCHANGED',probe=advice,released=released),indent=2))
        cache=ArtifactCache(run,out)
    # Resolve and verify the run root with the existing anchored guard.
    from fourstate_sync_remote import Guard
    guard = Guard()
    with guard.directory(str(run.relative_to(allowed))):
        pass
    guard.close()
    if args.reuse_archive_index:
        reuse_archive_index(args.reuse_archive_index)
    probe=out/'storage-roundtrip-probe.json'
    payload=(b'{"state":"01xz","address":6143}\n'*4096)
    probe.write_bytes(payload)
    archive(probe)
    if ArchivePath(probe).read_bytes()!=payload or not ArchivePath(probe).is_file() or ArchivePath(probe).stat().st_size!=len(payload):
        raise ValueError('Storage roundtrip failed')
    (out/'storage-selfcheck.json').write_text(json.dumps(dict(status='LOSSLESS_STORAGE_SELF_CHECK_PASS',
        bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
        logical_read=True,logical_stat=True,physical_single_link=True)))
    sys.path.insert(0, str(project/'scripts'))
    if args.stream_json:
        from fourstate_json_stream import build
        stream_reader, stream_build = build(out)
        import bootstrap_json_stream
        # Only this private process adapts the platform-specific reader factory.
        # Public audit, preflight, rewrite, checks and source hashes are intact.
        lock_dir = out/'json-stream-runtime'
        lock_dir.mkdir()
        (lock_dir/'chisel2lean-lock.json').write_text(json.dumps(dict(
            format='fourstate-private-exact-json-reader-v1', **stream_build), indent=2))
        bootstrap_json_stream.reader = lambda: stream_reader
        bootstrap_json_stream.INSTALL = lock_dir
    import yosys2lean as backend
    public_hash = fingerprint(project/'scripts/yosys2lean.py')[1]
    original_run = backend.run_logged
    original_checkpoint = backend.save_checkpoint

    def wait_for_heavy_job():
        if args.await_label:
            from fourstate_sync_common import identifier
            dependency=run/'reports'/identifier(args.await_label)/'execution.json'
            if not dependency.is_file():
                print(json.dumps(dict(event='waiting_before_heavy_stage',dependency=str(dependency))),flush=True)
                while not dependency.is_file():
                    time.sleep(5)
            if json.loads(dependency.read_bytes())['exit_code']!=0:
                raise ValueError('Prerequisite did not complete successfully: '+str(dependency))

    def save_checkpoint(path, *items, **options):
        result=original_checkpoint(path,*items,**options)
        if args.await_label and path.name=='flat-checkpoint.json':
            wait_for_heavy_job()
        return result

    def run_logged(argv, log, **options):
        phase=Path(str(log)).name
        if cache and cache.error:raise RuntimeError('Private cache adviser failed before native stage') from cache.error
        if str(log).split('/')[-1].startswith('yosys-'):
            wait_for_heavy_job()
        pipes=None;code=None
        if args.stream_output_json and phase in {'yosys-prepare','yosys-functional','yosys-export'}:
            from fourstate_backend_pipes import JSONPipes
            names={'yosys-prepare':['before.json'],'yosys-functional':['unconstrained.json'],
                   'yosys-export':['named.json','lowered.json']}[phase]
            pipes=JSONPipes(Path(options['cwd']),names)
        try:
            code = original_run(argv, log, **options)
        finally:
            if pipes:
                rows=pipes.finish(code if code is not None else 1)
                (out/(phase+'-json-stream.json')).write_text(json.dumps(dict(producer_exit_code=code,outputs=rows),indent=2))
                if code==0:
                    for row in rows:
                        if fingerprint(row['archive'],compressed=True)!=(row['bytes'],row['sha256']):
                            raise ValueError('Native streamed JSON archive roundtrip failed')
                        ARCHIVES[key(row['logical'])]=dict(archive=row['archive'],bytes=row['bytes'],sha256=row['sha256'],
                            archive_bytes=Path(row['archive']).stat().st_size,archive_sha256=fingerprint(row['archive'])[1],
                            source=row['logical'],transport='native-json-fifo-gzip-v1')
                    receipt()
        if code == 0 and str(log).split('/')[-1].startswith('yosys-'):
            stage = Path(options['cwd'])
            stage.relative_to(out)
            for name in sorted(JSON_NAMES):
                path = stage/name
                if key(path) not in ARCHIVES and path.is_file():
                    archive(path)
        return code

    backend.Path = ArchivePath
    backend.run_logged = run_logged
    backend.save_checkpoint = save_checkpoint
    shutil.copy2 = copy2
    sys.argv = [str(project/'scripts/yosys2lean.py'), *args.backend_args]
    if sys.argv[1:2] == ['--']:
        del sys.argv[1]
    try:
        if cache:cache.start()
        backend.main()
        if fingerprint(project/'scripts/yosys2lean.py')[1] != public_hash:
            raise ValueError('Public backend source changed')
        roots = sorted(out.glob('translation/*/artifacts/manifest.json'))
        if len(roots) != 1:
            raise ValueError('Exactly one compiled model required')
        receipt()
        (out/'model-reference.json').write_text(json.dumps(dict(status='CANONICAL_ARTIFACT_MODEL',
            root=str(roots[0].parent), backend_source_sha256=public_hash,
            adapter_sha256=fingerprint(Path(__file__))[1],
            duplicated_tree=False, lossless_json=True), indent=2))
    finally:
        shutil.copy2 = COPY
        receipt()
        if cache:
            try:cache.finish()
            finally:
                (out/'cache-advice-summary.json').write_text(json.dumps(dict(files=cache.counts,error=repr(cache.error) if cache.error else None,policy='Private artifact fdatasync/fadvise only; no limits or global policy changed'),indent=2))


if __name__ == '__main__':
    main()
