#!/usr/bin/env python3
"""Replay preserved native stage commands without rewriting compiler sources."""
import argparse,gzip,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import build
ROOT=build.ROOT
ORIGINAL=Path(json.loads((ROOT/'provenance/origin.json').read_text())['original_root'])

def translate(path):
    p=Path(path)
    return ROOT/'snapshot'/p.relative_to(ORIGINAL) if p.is_relative_to(ORIGINAL) else p

def sandbox(output,stage):
    return build.isolation(output)+['--ro-bind',str(ROOT/'snapshot'),str(ORIGINAL),
        '--bind',str(output),str(ORIGINAL/stage),'--chdir',str(ORIGINAL/stage),
        '--setenv','OMP_NUM_THREADS','1','--setenv','HOME','/tmp']

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',help='snapshot-relative stage directory, or native-route')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args();out=a.output.resolve()
    if a.stage=='native-route':
        stage=build.R/'probe/physical-model-full-model-mux-only-recovery-v1/route'
        data=json.loads((ROOT/'snapshot'/stage/'native-command.json').read_text())
        source=ROOT/'snapshot'/build.R/'probe/physical-model-full-model-mux-only-v1/solver/design.json.gz'
        if build.digest(source)!=data['input_gzip_sha256']:raise ValueError('Changed placed route input')
        if a.check_only:print('Pinned placed input and native route command found');return
        out.mkdir(parents=True,exist_ok=False)
        with gzip.open(source,'rb') as src,(out/'native-input.json').open('wb') as dst:shutil.copyfileobj(src,dst)
        if build.digest(out/'native-input.json')!=data['raw_input_sha256']:raise ValueError('Changed decompressed input')
        command=sandbox(out,stage)+data['argv']
        expected={'route.fasm':build.FASM_HASH};deadline=43200
    else:
        stage=Path(a.stage)
        if stage.is_absolute() or '..' in stage.parts:raise ValueError('Use a snapshot-relative stage')
        spec=json.loads((ROOT/'snapshot'/stage/'spec.json').read_text())
        for path,h in {**spec['input_hashes'],**spec['tool_hashes']}.items():
            if build.digest(translate(path))!=h:raise ValueError('Changed dependency '+path)
        if a.check_only:print('Verified inputs/tools for '+spec['name']);return
        out.mkdir(parents=True,exist_ok=False)
        command=sandbox(out,stage)
        suite=json.loads((ROOT/'snapshot'/build.R/'compiler-suite-stable-command.json').read_text())
        for key,value in {**suite['environment'],**spec.get('environment',{})}.items():command+=['--setenv',key,value]
        command+=['--setenv','PATH',suite['path'],*spec['argv']]
        accepted=ROOT/'snapshot'/stage/'success.json'
        expected=json.loads(accepted.read_text()).get('output_hashes',{}) if accepted.is_file() else {}
        deadline=spec['timeout_seconds']
    record=dict(command=command,stage=str(stage),status='running',original_workspace_visible=False)
    report=out/'replay-result.json'
    report.write_text(json.dumps(record,indent=2)+'\n');started=time.monotonic()
    try:
        with (out/'replay.log').open('w') as log:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=deadline)
        record.update(exit_code=result.returncode,seconds=time.monotonic()-started)
        mismatches=[];checked={}
        for name,h in expected.items():
            # Stage receipts normally use stage-relative output names.
            q=Path(name)
            if q.is_absolute():q=q.relative_to(ORIGINAL/stage)
            target=out/q
            actual=build.digest(target) if target.is_file() else None
            checked[str(q)]=actual
            if actual!=h:mismatches.append(str(q))
        record.update(output_hashes=checked,mismatches=mismatches,status='matched' if result.returncode==0 and not mismatches and expected else 'unverified-or-failed')
    except Exception as error:record.update(status='failed',error=repr(error))
    report.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record,indent=2))
    raise SystemExit(0 if record['status']=='matched' else 1)
if __name__=='__main__':main()
