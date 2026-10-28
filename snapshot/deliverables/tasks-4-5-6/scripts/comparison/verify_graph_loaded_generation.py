#!/usr/bin/env python3
"""Verification-only full generation through loader/bank/CDC RTL.

The memory peer is behavioral. This comparison grants no controller, PHY,
calibration, placement, timing, bitstream or board acceptance.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

D=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(D),str(D/'scripts/comparison')]
from compiler.build_core import verify_core_receipt
from compiler.stages import digest,_identity,_write_json
from graph_loaded_simulator import authenticate_loaded_simulator
from graph_rtl_runtime import decode_output

PROMPTS={'sequential':[1,2,3,4],'alternate':[50256,0,9,123]}


def _prepare_outputs(core,transport,build,snapshot,package,reference_source,work,report,extra=()):
    work,report=Path(work),Path(report)
    if work.is_symlink() or report.is_symlink():
        raise ValueError('verification outputs must not be symlinks')
    work,report=work.resolve(),report.resolve()
    protected=tuple(Path(p).resolve() for p in (core,transport,build,snapshot,package,reference_source,*extra))
    protected+=tuple(D/name for name in ('compiler','scripts','rtl','nix','TinyStories','calyx','model_inputs'))
    protected+=(D/'flake.nix',D/'flake.lock')
    if (any(report==p or report.is_relative_to(p) for p in protected) or
            any(work==p or work.is_relative_to(p) or p.is_relative_to(work) for p in protected)):
        raise ValueError('verification outputs overlap protected inputs')
    if report==work or work.is_relative_to(report):
        raise ValueError('verification report overlaps runtime directory')
    report.unlink(missing_ok=True)
    return work,report


def write_runtime_frames(layout,image,output):
    image,output=Path(image),Path(output)
    if output.is_symlink() or output.resolve()==image.resolve():
        raise ValueError('runtime frame output overlaps input or is a symlink')
    if (layout['data_width']!=576 or layout['address_width']!=25 or
            layout['frame_bytes']!=72 or layout['byte_order']!='little' or
            type(layout['frames']) is not int or layout['frames']<=0 or
            image.stat().st_size!=layout['frames']*72 or digest(image)!=layout['image_sha256']):
        raise ValueError('runtime image differs from authenticated native layout')
    with image.open('rb') as source,output.open('w') as destination:
        for _ in range(layout['frames']):
            frame=source.read(72)
            destination.write(f'{int.from_bytes(frame,"little"):0144x}\n')
    if digest(image)!=layout['image_sha256']:
        output.unlink(missing_ok=True)
        raise ValueError('runtime image changed during frame conversion')


def verify_loaded_generation(core,profile,transport,build,*,snapshot,package,
                             reference_source,work,report,timeout_seconds=7200):
    work,report=_prepare_outputs(core,transport,build,snapshot,package,reference_source,work,report,
        (profile.numerical_contract,profile.calyx_library))
    if timeout_seconds<=0:
        raise ValueError('positive numerical replay timeout required')
    binary,dependencies=authenticate_loaded_simulator(build,core,profile,transport)
    compiled=verify_core_receipt(core,profile)
    if work.exists() and any(work.iterdir()):
        raise ValueError('numerical replay requires a fresh runtime directory')
    # Expected answers are created only after the hardware build is authenticated.
    from compiler.frontend.package_inputs import load_compile_inputs
    from compiler.frontend.package_model import build_fixed_point_model
    from verify_closure_reference import _pinned_numpy_reference
    from verify_graph_model import verify_cpu_cases
    inputs=load_compile_inputs(snapshot,package,profile.numerical_contract)
    if inputs.materialized_sha256!=compiled['materialized_inputs_sha256']:
        raise ValueError('reference package/snapshot differs from compiled core')
    cls,reference_sources=_pinned_numpy_reference(reference_source)
    independent=cls(package)
    cpu_cases=verify_cpu_cases(build_fixed_point_model(inputs),independent,PROMPTS)
    core=Path(core).resolve();layout=json.loads((core/'ddr/layout.json').read_text())
    if layout['image_file']!='images.bin':
        raise ValueError('unsupported core image filename')
    image=core/'ddr/images.bin'
    if dependencies.get(str(image))!=layout['image_sha256']:
        raise ValueError('runtime image lacks compiled core provenance')
    work.mkdir(parents=True,exist_ok=True)
    frames=work/'frames.hex';write_runtime_frames(layout,image,frames)
    frame_hash=digest(frames);binary_hash=digest(binary);cases=[]
    for cpu in cpu_cases:
        label=cpu['case'];prompt=cpu['prompt'];tokens=[step['token'] for step in cpu['steps']]
        final_context=prompt+tokens[:-1]
        expected=independent.forward(final_context)[-1].copy()
        directory=work/label;directory.mkdir()
        shutil.copyfile(frames,directory/'frames.hex')
        (directory/'prompt.hex').write_text(''.join(f'{token:016x}\n' for token in prompt))
        prompt_hash=digest(directory/'prompt.hex')
        with (directory/'stdout.log').open('w') as stdout,(directory/'stderr.log').open('w') as stderr:
            try:
                result=subprocess.run([str(binary),'+OBSERVER_SECONDS=7200000',
                    '+OBSERVER_EVERY_EVALS=64','+ORDER=1','+DELAY=2'],cwd=directory,
                    stdout=stdout,stderr=stderr,timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _write_json(directory/'process-status.json',{'exit_code':None,
                    'completed':False,'timeout_seconds':timeout_seconds})
                raise
        _write_json(directory/'process-status.json',{'exit_code':result.returncode})
        stdout=(directory/'stdout.log').read_text();stderr=(directory/'stderr.log').read_text()
        if result.returncode or re.search(r'(?i)\b(fatal|error|warning)\b',stdout+stderr):
            raise RuntimeError('loaded RTL replay failed: '+label)
        actual_tokens=decode_output(stdout,16,64,prefix='TOKEN')
        actual_logits=decode_output(stdout,50257,64,prefix='LOGIT')
        if actual_tokens!=tokens or actual_logits!=[int(v)&((1<<64)-1) for v in expected.tolist()]:
            raise ValueError('loaded RTL differs from independent CPU: '+label)
        if (digest(directory/'frames.hex')!=frame_hash or digest(binary)!=binary_hash or
                digest(directory/'prompt.hex')!=prompt_hash):
            raise ValueError('runtime inputs or simulator changed during replay')
        authenticate_loaded_simulator(build,core,profile,transport)
        cases.append({'case':label,'prompt':prompt,'tokens':tokens,'logit_words_equal':50257,
                      'prompt_sha256':prompt_hash,
                      'stdout_sha256':digest(directory/'stdout.log'),
                      'stderr_sha256':digest(directory/'stderr.log')})
    for path,identity in {**dependencies,**reference_sources}.items():
        if digest(Path(path))!=identity:
            raise ValueError('numerical verification dependency changed')
    load_compile_inputs(snapshot,package,profile.numerical_contract)
    result={'schema_version':1,'scope':'Full sixteen-token/all-final-logit loaded-bank/CDC comparison; '
            'behavioral native memory, no physical/controller acceptance',
            'core_identity':compiled['identity_sha256'],'binary_sha256':binary_hash,
            'frames_sha256':frame_hash,'cpu_cases':cpu_cases,'cases':cases,
            'dependency_hashes':{**dependencies,**reference_sources},
            'verifier_sha256':digest(Path(__file__))}
    result['identity_sha256']=_identity(result)
    report.parent.mkdir(parents=True,exist_ok=True);_write_json(report,result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('core','transport','build','snapshot','package','reference-source','work','report',
                 'numerical-contract','calyx-library'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--timeout-seconds',type=int,default=7200)
    args=parser.parse_args(argv)
    _prepare_outputs(args.core,args.transport,args.build,args.snapshot,args.package,
                     args.reference_source,args.work,args.report,
                     (args.numerical_contract,args.calyx_library))
    from scripts.pipeline.export_graph_ddr_transport import read_profile
    profile=read_profile(args.core,args.numerical_contract,args.calyx_library)
    verify_loaded_generation(args.core,profile,args.transport,args.build,snapshot=args.snapshot,
        package=args.package,reference_source=args.reference_source,work=args.work,report=args.report,
        timeout_seconds=args.timeout_seconds)


if __name__=='__main__':
    main()
