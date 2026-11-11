"""Windows/local CLI for immutable, explicitly whitelisted four-state snapshots."""
import argparse
import datetime
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid
from fourstate_sync_common import (WORKSPACE, SOURCE_LIMIT, EXPORT_LIMIT, ARCHIVE_OVERHEAD,
    identifier, relative, source_path, encoded, loads, sha, records, make_zip, inspect_zip)

ROOT = Path(__file__).absolute().parents[1]
OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
           '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no', '-o', 'ConnectTimeout=15',
           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']


def fresh_id(prefix):
    return prefix+'-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dt%H%M%Sz')+'-'+uuid.uuid4().hex[:12]


def checked(base, name=None):
    base = Path(base).absolute()
    base.relative_to(ROOT)
    path = base.joinpath(*relative(name)) if name else base
    for entry in reversed([path, *path.parents]):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('local symlink/junction/reparse point refused: '+str(entry))
    return path


def source_file(name):
    """Resolve a frozen legacy script path to its categorized source file.

    Sync profiles keep their original archive paths because existing remote
    snapshots consume that layout. Locally, migrated implementations live in
    the verification or experiment subdirectories.
    """
    parts = relative(name)
    if len(parts) != 2 or parts[0] != 'scripts' or not parts[1].endswith('.py'):
        return checked(ROOT, name)
    legacy = checked(ROOT, name)
    if legacy.is_file():
        return legacy
    candidates = [
        ROOT/'scripts/verification/two_state'/parts[1],
        ROOT/'scripts/verification/four_state'/parts[1],
        ROOT/'scripts/experiments/four_state/runners'/parts[1],
    ]
    matches = [checked(path) for path in candidates if path.is_file()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError('ambiguous categorized source path: '+name)
    return legacy


def new_directory(path):
    path = checked(path)
    checked(path.parent).mkdir(parents=True, exist_ok=True)
    checked(path.parent)
    path.mkdir(exist_ok=False)
    return path


def new_file(path, data):
    path = checked(path)
    checked(path.parent).mkdir(parents=True, exist_ok=True)
    checked(path.parent)
    with path.open('xb') as stream:
        stream.write(data)


def stable_read(path, limit=SOURCE_LIMIT+ARCHIVE_OVERHEAD):
    path = checked(path)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (limit is not None and before.st_size > limit):
            raise ValueError('regular file within byte budget required: '+str(path))
        data = stream.read() if limit is None else stream.read(limit+1)
        after = os.fstat(stream.fileno())
    checked(path)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if len(data) != before.st_size or any(getattr(before,k)!=getattr(after,k) for k in fields):
        raise ValueError('file changed during read: '+str(path))
    return data


def git(*args):
    return subprocess.check_output(['git', '--no-optional-locks', '--literal-pathspecs', *args], cwd=str(ROOT))


def preview(profile='f3.1', snapshot='preview'):
    if profile not in ('f3.1','f4.6','f4.7','f4.7a','f5.1','f5.1a'):
        raise ValueError('only reviewed f3.1 / f4.6 / f4.7 / f4.7a / f5.1 / f5.1a profiles are available')
    config_path = checked(ROOT, 'verification/fourstate/sync-profile-'+profile+'.json')
    config_raw = stable_read(config_path)
    config = loads(config_raw)
    names = config['files']
    if not names or len(names) != len(set(n.casefold() for n in names)):
        raise ValueError('empty or duplicate source whitelist')
    rows = []
    for name in sorted(names):
        source_path(name)
        data = stable_read(source_file(name), None if profile in ('f5.1','f5.1a') else 2*1024**2)
        rows.append(dict(path=name, bytes=len(data), sha256=sha(data)))
    source_names = {source_file(name).relative_to(ROOT).as_posix() for name in names}
    status_names = sorted(set(names) | source_names)
    state = git('status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames', '--', *status_names)
    manifest = dict(format='fourstate-sync-source-v1', snapshot_id=identifier(snapshot), profile=profile,
                    profile_sha256=sha(config_raw), files=rows, bytes=sum(r['bytes'] for r in rows),
                    git=dict(head=git('rev-parse','HEAD').decode().strip(),
                             branch=git('branch','--show-current').decode().strip(),
                             selected_status_porcelain_z=state.decode('utf-8')),
                    scope=config.get('scope','Static audit reference set; not a runnable full four-state dependency closure.'))
    if profile in ('f5.1','f5.1a'):manifest['unrestricted']=True
    records(manifest, 'source')
    return manifest, config


def pack(profile='f3.1', snapshot=None):
    snapshot = identifier(snapshot or fresh_id('snap'))
    manifest, config = preview(profile, snapshot)
    folder = new_directory(ROOT/'.cache/fourstate-sync'/snapshot)
    try:
        payloads = {}
        for row in manifest['files']:
            content = stable_read(source_file(row['path']),None if manifest.get('unrestricted') else SOURCE_LIMIT+ARCHIVE_OVERHEAD)
            if len(content)!=row['bytes'] or sha(content)!=row['sha256']:
                raise ValueError('source changed after preview: '+row['path'])
            new_file(folder/'source'/row['path'], content)
            payloads[row['path']] = content
        # Detect edits across the copy window, not just within a single read.
        for row in manifest['files']:
            if sha(stable_read(source_file(row['path']),None if manifest.get('unrestricted') else SOURCE_LIMIT+ARCHIVE_OVERHEAD))!=row['sha256']:
                raise ValueError('source changed while packing: '+row['path'])
        data = make_zip(manifest,payloads,'source')
        inspect_zip(data,'source',sha(encoded(manifest)),[r['path'] for r in manifest['files']])
        new_file(folder/'manifest.json',encoded(manifest))
        new_file(folder/'bundle.zip',data)
        patch = git('diff','--binary','--',*[r['path'] for r in manifest['files']])
        new_file(folder/'selected-working-tree.patch',patch)
        receipt = dict(status='PACKED',snapshot_id=snapshot,manifest_sha256=sha(encoded(manifest)),
                       archive_sha256=sha(data),archive_bytes=len(data),files=len(payloads),
                       uncompressed_bytes=manifest['bytes'],git_diff_sha256=sha(patch))
        new_file(folder/'receipt.json',encoded(receipt))
        new_file(folder/'COMPLETE',encoded(dict(receipt_sha256=sha(encoded(receipt)))))
        return receipt
    except BaseException as error:
        new_file(folder/'FAILED.json',encoded(dict(error=str(error))))
        raise


def frozen(snapshot, check_current=True):
    folder = checked(ROOT/'.cache/fourstate-sync',identifier(snapshot))
    marker = loads(stable_read(folder/'COMPLETE'))
    raw = stable_read(folder/'receipt.json')
    if sha(raw)!=marker['receipt_sha256']:
        raise ValueError('local receipt changed')
    receipt=loads(raw)
    data=stable_read(folder/'bundle.zip',None)
    if sha(data)!=receipt['archive_sha256']:
        raise ValueError('local archive changed')
    manifest,raw,payloads=inspect_zip(data,'source',receipt['manifest_sha256'])
    if manifest['snapshot_id']!=snapshot or stable_read(folder/'manifest.json')!=raw:
        raise ValueError('local snapshot identity changed')
    if check_current:
        for row in manifest['files']:
            if sha(stable_read(source_file(row['path']),None if manifest.get('unrestricted') else SOURCE_LIMIT+ARCHIVE_OVERHEAD))!=row['sha256']:
                raise ValueError('local source changed since Pack; make a new snapshot: '+row['path'])
    return folder,manifest,receipt,payloads


def remote(request, payloads=None):
    sources={}
    for name in ('fourstate_sync_common.py','fourstate_sync_remote.py'):
        key='scripts/'+name
        sources[name] = (payloads[key] if payloads is not None else stable_read(ROOT/key)).decode('utf-8')
    # Only trusted, hash-verified local helper source is executable. Manifests
    # and paths are passed as data, never shell fragments or Python programs.
    code = "import sys,types\nm=types.ModuleType('fourstate_sync_common')\n"
    code += 'exec('+repr(sources['fourstate_sync_common.py'])+',m.__dict__)\n'
    code += "sys.modules['fourstate_sync_common']=m\nn={'__name__':'sync_receiver'}\n"
    code += 'exec('+repr(sources['fourstate_sync_remote.py'])+',n)\n'
    code += 'n[\'main\']('+repr(request)+')\n'
    log=new_directory(ROOT/'.cache/fourstate-sync/operations'/fresh_id(request['operation']))
    new_file(log/'stdin.py',code.encode('utf-8'))
    command=['ssh.exe','-T','-a','-x',*OPTIONS,'amax','/usr/bin/python3 -I -B -S -']
    new_file(log/'command.json',encoded(dict(argv=command,stdin_sha256=sha(code.encode()),request=request)))
    try:
        result=subprocess.run(command,input=code.encode('utf-8'),capture_output=True,timeout=None if request.get('unrestricted') else 60)
    except subprocess.TimeoutExpired as error:
        new_file(log/'stdout.log',error.stdout or b'')
        new_file(log/'stderr.log',error.stderr or b'')
        new_file(log/'result.json',encoded(dict(exit_code=None,timeout=True)))
        raise
    new_file(log/'stdout.log',result.stdout)
    new_file(log/'stderr.log',result.stderr)
    new_file(log/'result.json',encoded(dict(exit_code=result.returncode)))
    if result.returncode:
        raise RuntimeError('remote operation failed; preserved local receipt: '+str(log)+'\n'+result.stderr.decode('utf-8',errors='replace'))
    value=loads(result.stdout)
    return value


def scp(source,destination,log,unrestricted=False):
    command=['scp.exe','-B',*OPTIONS,str(source),str(destination)]
    new_file(log/'scp-command.json',encoded(dict(argv=command)))
    try:
        result=subprocess.run(command,capture_output=True,timeout=None if unrestricted else 120)
    except subprocess.TimeoutExpired as error:
        new_file(log/'scp-stdout.log',error.stdout or b'')
        new_file(log/'scp-stderr.log',error.stderr or b'')
        new_file(log/'scp-result.json',encoded(dict(exit_code=None,timeout=True)))
        raise
    new_file(log/'scp-stdout.log',result.stdout)
    new_file(log/'scp-stderr.log',result.stderr)
    new_file(log/'scp-result.json',encoded(dict(exit_code=result.returncode)))
    if result.returncode:
        raise RuntimeError('SCP failed; partial transfer retained: '+str(log))


def push(snapshot):
    folder,manifest,receipt,payloads=frozen(identifier(snapshot))
    transfer=fresh_id('transfer')
    log=new_directory(folder/'transfers'/transfer)
    request=dict(operation='prepare',snapshot_id=snapshot,transfer_id=transfer,
                 archive_sha256=receipt['archive_sha256'],manifest_sha256=receipt['manifest_sha256'],
                 paths=[r['path'] for r in manifest['files']])
    if manifest.get('unrestricted'):
        request.update(unrestricted=True,archive_bytes=receipt['archive_bytes'],source_bytes=manifest['bytes'])
    ready=remote(request,payloads)
    new_file(log/'prepare.json',encoded(ready))
    if ready['status']=='ALREADY_DEPLOYED':
        return ready
    expected=WORKSPACE+'/incoming/'+transfer+'/bundle.zip'
    if ready['status']!='READY' or ready['upload_path']!=expected:
        raise ValueError('unexpected upload destination')
    scp(folder/'bundle.zip','amax:'+expected,log,unrestricted=manifest.get('unrestricted',False))
    received=remote(dict(operation='receive',transfer_id=transfer,unrestricted=manifest.get('unrestricted',False)),payloads)
    if received['manifest_sha256']!=receipt['manifest_sha256']:
        raise ValueError('server verified a different snapshot')
    new_file(log/'receive.json',encoded(received))
    return received


def export_results(run,snapshot,selection,export=None):
    selected=loads(stable_read(checked(Path(selection).absolute()),1024**2))
    request=dict(operation='export',run_id=identifier(run),snapshot_id=identifier(snapshot),
                 export_id=identifier(export or fresh_id('export')),files=selected['files'],
                 retained_assets=selected.get('retained_assets',[]))
    if selected.get('unrestricted'):request['unrestricted']=True
    return remote(request)


def pull(run,export,unrestricted=False):
    run,export=identifier(run),identifier(export)
    receipt=remote(dict(operation='export-info',run_id=run,export_id=export,unrestricted=unrestricted))
    expected=WORKSPACE+'/runs/'+run+'/exports/'+export+'/bundle.zip'
    unlimited=receipt.get('unrestricted',False)
    if receipt['download_path']!=expected or (not unlimited and receipt['archive_bytes']>EXPORT_LIMIT+ARCHIVE_OVERHEAD):
        raise ValueError('unexpected download destination/size')
    folder=new_directory(ROOT/'runs/fourstate'/run/'imports'/export)
    new_file(folder/'export-receipt.json',encoded(receipt))
    scp('amax:'+expected,folder/'bundle.zip',folder,unrestricted=unlimited)
    data=stable_read(folder/'bundle.zip',None if unlimited else EXPORT_LIMIT+ARCHIVE_OVERHEAD)
    if sha(data)!=receipt['archive_sha256']:
        raise ValueError('downloaded archive SHA256 mismatch')
    manifest,raw,payloads=inspect_zip(data,'export',receipt['manifest_sha256'])
    if (manifest['run_id'],manifest['export_id'],manifest['snapshot_id'])!=(run,export,receipt['snapshot_id']):
        raise ValueError('downloaded identity mismatch')
    after=remote(dict(operation='export-info',run_id=run,export_id=export,unrestricted=unlimited))
    if after!=receipt:
        raise ValueError('export changed during download')
    for name,content in payloads.items():
        new_file(folder/'files'/name,content)
    new_file(folder/'manifest.json',raw)
    complete=dict(status='IMPORTED',run_id=run,export_id=export,snapshot_id=manifest['snapshot_id'],
                  files=len(payloads),bytes=manifest['bytes'],archive_sha256=sha(data),
                  manifest_sha256=sha(raw),local_directory=str(folder),retained_assets=manifest['retained_assets'])
    new_file(folder/'COMPLETE',encoded(complete))
    return complete


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['preview','pack','push','verify','status','export','pull'])
    parser.add_argument('--profile',default='f3.1')
    parser.add_argument('--snapshot-id')
    parser.add_argument('--run-id')
    parser.add_argument('--export-id')
    parser.add_argument('--selection')
    args=parser.parse_args()
    if args.action=='preview':
        manifest,config=preview(args.profile)
        result=dict(manifest=manifest,exclusions=config['excluded'],remote_write_root=WORKSPACE,
                    note='Read-only preview. No staging, upload, installation or task execution.')
    elif args.action=='pack':
        result=pack(args.profile,args.snapshot_id)
    elif args.action=='push':
        result=push(identifier(args.snapshot_id))
    elif args.action=='verify':
        result=remote(dict(operation='verify',snapshot_id=identifier(args.snapshot_id)))
    elif args.action=='status':
        result=remote(dict(operation='status'))
    elif args.action=='export':
        if not args.selection:
            parser.error('export requires --selection')
        result=export_results(args.run_id,args.snapshot_id,args.selection,args.export_id)
    else:
        result=pull(args.run_id,args.export_id)
    print(encoded(result).decode(),end='')


if __name__=='__main__':
    main()
