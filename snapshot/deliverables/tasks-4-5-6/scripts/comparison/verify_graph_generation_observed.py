#!/usr/bin/env python3
"""Exact per-step public-output comparison of an authenticated RTL simulator."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

D=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(D),str(D/'scripts/comparison')]
from compiler.stages import digest,verify_stage_receipt
from graph_rtl_runtime import decode_output,install_frozen_images
from verify_graph_generation_rtl import authenticated_rtl

# Independent-reference identity manifest at the frozen goal base6dbb75bd.
NUMPY_IDENTITY_SHA256='258bbcc081a5166b8f302ff6d736730f1743d7fa448413bae6c3774b9f5ff533'


def compare_observations(directory,stdout,references,*,logit_words):
    """Check every captured bit, ordered step and earliest-index token."""
    lines=[line for line in stdout.splitlines() if line.startswith('STEP ')]
    if not references or len(lines)!=len(references):
        raise ValueError('incomplete or duplicate step observations')
    results=[]
    for step,(line,(token,expected)) in enumerate(zip(lines,references,strict=True)):
        match=re.fullmatch(r'STEP (\d+) ([0-9a-fA-F]{16}) (\d+) (step-\d{2}\.hex)',line)
        filename=f'step-{step:02d}.hex'
        if not match or (int(match[1]),int(match[3]),match[4])!=(step,logit_words,filename):
            raise ValueError('malformed, reordered or wrong-geometry step observation')
        path=Path(directory)/filename
        if path.is_symlink() or not path.is_file():raise ValueError('missing or linked snapshot')
        words=path.read_text().splitlines()
        if len(words)!=logit_words or any(re.fullmatch(r'[0-9a-fA-F]{16}',s) is None for s in words):
            raise ValueError('malformed or incomplete snapshot')
        actual=[int(s,16) for s in words]
        wanted=[int(v)&((1<<64)-1) for v in expected]
        if len(wanted)!=logit_words:raise ValueError('reference geometry differs')
        for index,(a,b) in enumerate(zip(actual,wanted,strict=True)):
            if a!=b:raise ValueError(f'step{step} logit{index}: RTL={a:016x}, CPU={b:016x}')
        earliest=max(range(logit_words),key=lambda j:int(expected[j]))
        if int(match[2],16)!=int(token) or int(token)!=earliest:
            raise ValueError('token differs from CPU or earliest winning index')
        results.append({'step':step,'context_length':4+step,'token':int(token),
                        'logit_words_equal':logit_words,'snapshot_sha256':digest(path)})
    return results


def validate_observer_exit(code,stderr,*,steps,stdout=''):
    if not 1<=steps<=16:raise ValueError('invalid requested step count')
    if steps==16:
        if code!=0:raise ValueError('generation did not complete')
    elif code!=125 or 'OBSERVER diagnostic step limit; generation unfinished' not in stderr:
        raise ValueError('no explicit completed-step diagnostic stop')
    if (re.search(r'(?i)\b(fatal|error|warning)\b',stdout+'\n'+stderr) or
            '$readmem file not found' in stdout):
        raise ValueError('simulator reported diagnostics')


def verify_runtime_inputs(directory,expected):
    identities={}
    for name,identity in expected.items():
        if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('invalid runtime input path')
        path=Path(directory)/name
        if path.is_symlink() or not path.is_file() or digest(path)!=identity:
            raise ValueError('runtime input changed: '+name)
        identities[str(path.resolve())]=identity
    return identities


def verify_reference_sources(delivery,frozen_dependencies):
    identities={}
    for name in ('compiler/frontend/package_inputs.py','compiler/frontend/package_model.py',
                 'compiler/frontend/fixed_point_ops.py','compiler/generation.py','TinyStories/serial_gemv_boundary.py'):
        frozen=[identity for path,identity in frozen_dependencies.items()
                if Path(path).is_absolute() and path.endswith('/'+name)]
        path=Path(delivery)/name
        if len(frozen)!=1 or path.is_symlink() or digest(path)!=frozen[0]:
            raise ValueError('reference implementation differs from frozen compiler: '+name)
        identities[str(path.resolve())]=frozen[0]
    return identities


def full_goal_scope_validated(cases):
    return (len(cases)==2 and {c['case'] for c in cases}=={'sequential','alternate'}
            and all(c['generation_complete'] is True for c in cases))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','binary','snapshot','package','reference-source','build-dir','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--synthesis-root',type=Path)
    parser.add_argument('--work-dir',type=Path)
    parser.add_argument('--steps',type=int,default=16)
    parser.add_argument('--seconds',type=int,default=7200)
    parser.add_argument('--case',choices=('sequential','alternate','both'),default='both')
    args=parser.parse_args()
    protected=[args.root,args.binary,args.snapshot,args.package,args.reference_source,args.build_dir]
    if args.synthesis_root is not None:protected.append(args.synthesis_root)
    output=args.output.resolve()
    work=args.work_dir.resolve() if args.work_dir else args.output.parent.resolve()/'observed-runtime'
    if args.output.is_symlink():raise ValueError('output symlink is forbidden')
    if (output==work or output.is_relative_to(work) or
            any(output==p.resolve() or output.is_relative_to(p.resolve()) for p in protected)):
        raise ValueError('output must not overlap input/build evidence')
    args.output.unlink(missing_ok=True)
    if not 1<=args.steps<=16 or not 1<=args.seconds<=7_200_000:
        raise ValueError('invalid runtime limit')
    if args.synthesis_root is None:raise ValueError('selected native synthesis export is required')
    build=args.build_dir.resolve();binary=args.binary.resolve()
    dependencies=verify_stage_receipt(build)
    spec=json.loads((build/'spec.json').read_text())
    if (spec['name']!='graph-generation-observer-simulator' or
            dependencies.get(str(binary))!=digest(binary)):
        raise ValueError('binary is not the authenticated observer simulator')
    rtl,synthesis_identity=authenticated_rtl(args.root,args.synthesis_root)
    if spec['input_hashes'].get(str(rtl))!=digest(rtl):raise ValueError('different RTL selected')
    from compiler.generation_driver import _verified_artifact
    checked,_=_verified_artifact(build,('graph-generation-observer-simulator',),'verilator/simulator')
    if checked!=binary:raise ValueError('binary is not the declared simulator output')
    if any(work==p.resolve() or work.is_relative_to(p.resolve()) for p in protected):
        raise ValueError('runtime work must not overlap inputs/build')
    profile=json.loads((args.root/'profile.json').read_text())
    dependencies.update(verify_reference_sources(D,profile['dependency_hashes']))
    oracle=D/'artifacts/reference/tinystories-1m-fixed-logits-oracle.json'
    if digest(oracle)!=NUMPY_IDENTITY_SHA256:raise ValueError('independent reference identities changed')
    from compiler.frontend.package_inputs import load_compile_inputs
    from compiler.frontend.package_model import build_fixed_point_model
    from compiler.generation import GenerationStep
    from verify_closure_reference import _pinned_numpy_reference
    import torch
    import numpy as np
    inputs=load_compile_inputs(args.snapshot,args.package,D/'compiler/contracts/tinystories-1m-fixed-point.json')
    if inputs.contract_sha256!=profile['numerical_contract_sha256']:
        raise ValueError('CPU inputs differ from compiled numerical contract')
    eager=GenerationStep(build_fixed_point_model(inputs))
    cls,reference_sources=_pinned_numpy_reference(args.reference_source);reference=cls(args.package)
    dependencies.update({str((args.reference_source/name).resolve()):entry['sha256']
                         for name,entry in reference_sources.items()})
    for p in (Path(__file__),D/'compiler/frontend/package_inputs.py',
              D/'compiler/frontend/package_model.py',D/'compiler/frontend/fixed_point_ops.py',
              D/'TinyStories/serial_gemv_boundary.py',D/'compiler/generation.py',
              D/'scripts/comparison/verify_closure_reference.py',
              D/'artifacts/reference/tinystories-1m-fixed-logits-oracle.json',
              D/'scripts/comparison/graph_rtl_runtime.py',D/'compiler/stages.py'):
        dependencies[str(p)]=digest(p)
    for directory in (args.snapshot,args.package):
        dependencies.update({str(p.resolve()):digest(p) for p in directory.iterdir() if p.is_file()})
    bindings=json.loads((args.root/'bindings.json').read_text())['bindings']
    cases=[]
    for label,prompt in (('sequential',[1,2,3,4]),('alternate',[50256,0,9,123])):
        if args.case not in ('both',label):continue
        directory=work/label
        if directory.exists():raise ValueError('runtime case must be fresh; preserve previous evidence')
        directory.mkdir(parents=True)
        context=list(prompt);references=[]
        with torch.no_grad():
            for step in range(args.steps):
                expected=reference.forward(context)[-1].copy()
                token,logits=eager(torch.tensor([context],dtype=torch.int64))
                if (expected.dtype!=np.dtype('int64') or expected.shape!=(50257,) or
                        logits.dtype!=torch.int64 or list(logits.shape)!=[50257] or
                        token.dtype!=torch.int64 or token.ndim!=0 or
                        not np.array_equal(logits.numpy(),expected) or int(token)!=int(expected.argmax())):
                    raise ValueError(f'CPU references disagree at {label}/step{step}')
                references.append((int(token),expected));context.append(int(token))
        frozen=install_frozen_images(bindings,args.root,directory)
        prompt_bytes=''.join(f'{token:016x}\n' for token in prompt).encode()
        (directory/'prompt.hex').write_bytes(prompt_bytes)
        runtime_inputs={**frozen,'prompt.hex':hashlib.sha256(prompt_bytes).hexdigest()}
        dependencies.update(verify_runtime_inputs(directory,runtime_inputs))
        command=[str(binary),'+OBSERVER_CAPTURE_STEPS=1',f'+OBSERVER_SECONDS={args.seconds}']
        if args.steps<16:command.append(f'+OBSERVER_STOP_AFTER_STEPS={args.steps}')
        (directory/'command.json').write_text(json.dumps(command,indent=2)+'\n')
        print(label,'CPU references agree; starting frozen RTL',flush=True)
        with (directory/'stdout.log').open('w') as stdout,(directory/'stderr.log').open('w') as stderr:
            result=subprocess.run(command,cwd=directory,stdout=stdout,stderr=stderr,timeout=args.seconds+120)
        out=(directory/'stdout.log').read_text();err=(directory/'stderr.log').read_text()
        (directory/'exit.json').write_text(json.dumps({'returncode':result.returncode})+'\n')
        validate_observer_exit(result.returncode,err,steps=args.steps,stdout=out)
        verify_runtime_inputs(directory,runtime_inputs)
        steps=compare_observations(directory,out,references,logit_words=50257)
        if args.steps==16:
            if decode_output(out,16,64,prefix='TOKEN')!=[t for t,_ in references]:
                raise ValueError('final public token buffer differs')
            if decode_output(out,50257,64,prefix='LOGIT')!=[int(v)&((1<<64)-1) for v in references[-1][1]]:
                raise ValueError('final public logits buffer differs')
        for p in directory.iterdir():
            if not p.is_file():continue
            identity=digest(p)
            if str(p) in dependencies and dependencies[str(p)]!=identity:
                raise ValueError('runtime evidence changed: '+str(p))
            dependencies[str(p)]=identity
        cases.append({'case':label,'prompt':prompt,'steps':steps,'frozen_images':frozen,
                      'generation_complete':args.steps==16})
        print(label,len(steps),'actual-model forward passes match both CPU references',flush=True)
    verify_stage_receipt(build);authenticated_rtl(args.root,args.synthesis_root)
    if not all(digest(Path(p))==h for p,h in dependencies.items()):raise ValueError('verification inputs changed')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'scope':'Exact actual-model per-step comparisons; finite inputs, no formal/physical acceptance',
        'cases':cases,'full_generation_validated':full_goal_scope_validated(cases),
        'rtl_sha256':digest(rtl),'synthesis_identity':synthesis_identity,
        'dependency_hashes':dependencies},indent=2)+'\n')


if __name__=='__main__':main()
