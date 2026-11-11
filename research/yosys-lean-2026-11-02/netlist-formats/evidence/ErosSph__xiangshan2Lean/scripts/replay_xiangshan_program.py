"""Replay a recorded original-RTL program in Lean and compare every port sample.

This phase establishes finite RTL/Lean port agreement. Instruction/ISA and
internal commit-event acceptance are recorded separately, never inferred here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from compact_request import csv_inputs, write_compact
from server_bundle import read, verify_bundle, write
from yosys_hierarchy import digest


def replay(root, work, program, runner, validation, audit_every):
    verify_bundle(root)
    engine=read(runner/'manifest.json');receipt=read(validation)
    if receipt['status']!='PASS' or receipt['runner_manifest_sha256']!=digest(runner/'manifest.json'):
        raise ValueError('Runner has no matching successful regression receipt')
    executable=Path(engine['executable'])
    if digest(executable)!=engine['executable_sha256']:
        raise ValueError('Runner executable changed')
    for name,key in [('Functional.lean','runtime_sha256'),('Incremental.lean','incremental_source_sha256')]:
        if digest(runner/'Chisel2Lean'/name)!=engine[key]:raise ValueError('Runner source changed')
    if 'progressive_source_sha256' in engine:
        if digest(runner/'Chisel2Lean/Progressive.lean')!=engine['progressive_source_sha256']:
            raise ValueError('Progressive runner source changed')
    if 'compiled_source_sha256' in engine and digest(runner/'Chisel2Lean/Compiled.lean')!=engine['compiled_source_sha256']:
        raise ValueError('Verified inline helpers changed')
    model=read(work/'model/manifest.json')
    if not model['lean_checked'] or model['table_runtime_sha256']!=engine['runtime_sha256']:
        raise ValueError('Expected a Lean-checked model with the same pure evaluator')
    binary=work/'model'/model['table']['binary']
    if digest(binary)!=model['artifact_sha256'][model['table']['binary']]:raise ValueError('Circuit packet changed')
    rtl=read(program/'execution.json')
    if rtl['status']!='RTL_PROGRAM_COMPLETED' or rtl['signature']!=1 or rtl['committed']==0:
        raise ValueError('Original RTL program has not completed successfully')
    directory=program/'lean-port-replay'
    directory.mkdir(exist_ok=False)
    ports=read(root/'scenario/ports.json')['ports']
    inputs=[p['name'] for p in ports if p['direction']=='input']
    outputs=[p['name'] for p in ports if p['direction']=='output']
    request=directory/'request.jsonl'
    prepared=write_compact(root/'scenario/request.jsonl',request,inputs,csv_inputs(program/'inputs.csv'),audit_every)
    if prepared['samples']!=rtl['samples']:raise ValueError('Incomplete recorded physical input trace')
    report={'status':'RUNNING','samples_expected':rtl['samples'],'outputs':len(outputs),
            'request':prepared,'program_sha256':digest(program/'program.bin'),
            'program_elf_sha256':digest(program/'program.elf'),
            'circuit_sha256':digest(binary),'runner_manifest_sha256':digest(runner/'manifest.json'),
            'validation_sha256':digest(validation),'rtl_input_sha256':digest(program/'inputs.csv'),
            'rtl_output_sha256':digest(program/'outputs.csv'),'original_hardware_modified':False,
            'isa_comparison':'NOT_RUN','lean_commit_event_comparison':'NOT_RUN','universal_equivalence':'NOT_PROVED'}
    write(directory/'summary.json',report)
    began=time.monotonic();count=0;hash_output=hashlib.sha256()
    command=[str(executable),str(binary),str(request)]
    write(directory/'command.json',{'command':command,'cwd':str(runner)})
    with (directory/'stderr.log').open('w',encoding='utf-8') as errors, \
         (directory/'stdout.jsonl').open('w',encoding='utf-8',newline='\n') as saved, \
         (program/'outputs.csv').open(encoding='utf-8',newline='') as reference:
        process=subprocess.Popen(command,cwd=runner,stdout=subprocess.PIPE,stderr=errors,text=True,encoding='utf-8')
        write(directory/'process.json',{'pid':process.pid,'command':command})
        reader=iter(csv.DictReader(reference))
        try:
            for line in process.stdout:
                saved.write(line);hash_output.update(line.encode('utf-8'))
                actual=json.loads(line);expected=next(reader,None)
                if expected is None or actual.get('cycle')!=count or int(expected['sample'])!=count:
                    raise ValueError('Missing/reordered/extra sample at '+str(count))
                for phase in ['pre','post']:
                    values=actual.get(phase,{})
                    if set(values)!=set(outputs):raise ValueError('Output port set differs at '+str(count))
                    differences={name:{'lean':values[name],'rtl':int(expected[name],0)}
                                 for name in outputs if values[name]!=int(expected[name],0)}
                    if differences:
                        write(directory/'counterexample.json',{'sample':count,'phase':phase,'differences':differences})
                        raise ValueError('RTL/Lean output mismatch at sample '+str(count))
                count+=1
                if count%256==0:
                    saved.flush();elapsed=time.monotonic()-began
                    write(directory/'progress.json',{'samples':count,'expected':rtl['samples'],'elapsed_seconds':elapsed})
                    print(f'PORT REPLAY {count}/{rtl["samples"]} elapsed={elapsed:.1f}s',flush=True)
            code=process.wait()
            if code or count!=rtl['samples'] or next(reader,None) is not None:
                raise RuntimeError(f'Incomplete Lean program replay: exit={code}, samples={count}')
            if digest(request)!=prepared['request_sha256'] or digest(binary)!=report['circuit_sha256'] or \
               digest(program/'inputs.csv')!=report['rtl_input_sha256'] or \
               digest(program/'outputs.csv')!=report['rtl_output_sha256'] or digest(executable)!=engine['executable_sha256']:
                raise ValueError('Recorded input/output, model or executable changed during replay')
            report.update(status='RTL_LEAN_PORTS_PASS',samples=count,output_comparisons=count*2*len(outputs),
                          elapsed_seconds=time.monotonic()-began,lean_stdout_sha256=hash_output.hexdigest())
        except Exception as error:
            if process.poll() is None:process.terminate();process.wait(timeout=20)
            report.update(status='FAILED',samples=count,error=str(error))
            raise
        finally:
            process.stdout.close()
            write(directory/'summary.json',report)
    print(json.dumps({k:report[k] for k in ['status','samples','output_comparisons','elapsed_seconds']},indent=2),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['bundle','work','program','runner','validation']:
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--audit-every',type=int,default=256)
    args=parser.parse_args()
    if args.audit_every<1:parser.error('Enable periodic complete-evaluation audits')
    replay(args.bundle.resolve(),args.work.resolve(),args.program.resolve(),args.runner.resolve(),
           args.validation.resolve(),args.audit_every)
