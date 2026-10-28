#!/usr/bin/env python3
"""Verification-only two-prompt sixteen-step public-interface RTL comparison."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
D = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(D), str(D/'scripts/comparison')]
from graph_rtl_runtime import install_frozen_images, decode_output


def authenticated_rtl(root, synthesis_root=None):
    """Resolve the exact compiled RTL; provenance is separate from semantics."""
    from compiler.generation_driver import _verified_artifact
    from compiler.stages import digest, _identity
    root = Path(root).resolve()
    if synthesis_root is None:
        return _verified_artifact(root/'initialized/sv', ('sv',), 'kernel.sv')[0], None
    synthesis_root = Path(synthesis_root).resolve()
    document = json.loads((synthesis_root/'core-synthesis.json').read_text())
    if (document['schema_version'] != 1 or document['identity_sha256'] != _identity(
            {k:v for k,v in document.items() if k != 'identity_sha256'})):
        raise ValueError('invalid synthesis receipt identity')
    core_path = root/'core.json'
    core = json.loads(core_path.read_text())
    if (document['core_identity_sha256'] != core['identity_sha256'] or
            document['profile_sha256'] != core['profile_sha256'] or
            document['dependency_hashes'].get(str(core_path)) != digest(core_path)):
        raise ValueError('synthesis export belongs to a different or changed core')
    for name, expected in document['dependency_hashes'].items():
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or digest(path) != expected:
            raise ValueError('changed synthesis dependency: '+name)
    rtl, _ = _verified_artifact(synthesis_root/'sv', ('sv',), 'kernel.sv')
    if (Path(document['output']) != rtl or
            document['dependency_hashes'].get(str(rtl)) != digest(rtl)):
        raise ValueError('synthesis receipt does not authenticate its native output')
    return rtl, document['identity_sha256']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','binary','snapshot','package','reference-source','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--build-dir',type=Path)
    parser.add_argument('--synthesis-root',type=Path,
                        help='authenticate and compare the native synthesis export')
    parser.add_argument('--work-dir',type=Path)
    parser.add_argument('--timeout',type=int,default=7200)
    args=parser.parse_args()
    # Validate before invalidating stale acceptance: the report must never
    # replace a computation input, an authenticated build artifact, or a link.
    if args.output.is_symlink():
        raise ValueError('comparison output must not be a symlink')
    output = args.output.resolve()
    protected = (args.root, args.snapshot, args.package, args.reference_source)
    if args.synthesis_root is not None:
        protected += (args.synthesis_root,)
    if args.build_dir is not None:
        protected += (args.build_dir,)
    elif args.binary.is_file():
        # A missing executable does not establish an inferred build directory.
        # In particular, a missing sibling path must not reserve all of /tmp.
        protected += (args.binary.parent.parent,)
    if (output == args.binary.resolve() or any(
            output == path.resolve() or output.is_relative_to(path.resolve())
            for path in protected)):
        raise ValueError('comparison output must not overlap input or build evidence')
    args.output.unlink(missing_ok=True)
    root,binary=args.root.resolve(),args.binary.resolve()
    if not binary.is_file():
        raise RuntimeError('Compile hardware before creating runtime/reference cases')
    from compiler.generation_driver import _verified_artifact
    from compiler.stages import digest
    build=args.build_dir.resolve() if args.build_dir else binary.parent.parent
    authenticated,_=_verified_artifact(build,('graph-generation-simulator',),'verilator/simulator')
    if authenticated!=binary:
        raise ValueError('simulator does not match authenticated build')
    rtl,synthesis_identity=authenticated_rtl(root,args.synthesis_root)
    build_spec=json.loads((build/'spec.json').read_text())
    if build_spec['input_hashes'].get(str(rtl))!=digest(rtl):
        raise ValueError('simulator does not declare the selected RTL')
    import torch
    from compiler.frontend.package_inputs import load_compile_inputs
    from compiler.frontend.package_model import build_fixed_point_model
    from compiler.generation import GenerationStep
    from verify_closure_reference import _pinned_numpy_reference
    inputs=load_compile_inputs(args.snapshot,args.package,D/'compiler/contracts/tinystories-1m-fixed-point.json')
    eager=GenerationStep(build_fixed_point_model(inputs))
    cls,reference_sources=_pinned_numpy_reference(args.reference_source);reference=cls(args.package)
    bindings=json.loads((root/'bindings.json').read_text())['bindings']
    work=args.work_dir.resolve() if args.work_dir else root/'runtime-verification'
    records=[];binary_hash=digest(binary)
    for label,prompt in [('sequential',[1,2,3,4]),('alternate',[50256,0,9,123])]:
        context=list(prompt);tokens=[]
        with torch.no_grad():
            for _ in range(16):
                expected=torch.from_numpy(reference.forward(context)[-1].copy())
                token,logits=eager(torch.tensor([context],dtype=torch.int64))
                if not torch.equal(logits,expected) or int(token)!=int(expected.argmax()):
                    raise ValueError('CPU references disagree: '+label)
                tokens.append(int(token));context.append(int(token))
        directory=work/label;directory.mkdir(parents=True,exist_ok=True)
        frozen=install_frozen_images(bindings,root,directory)
        (directory/'prompt.hex').write_text(''.join(f'{token:016x}\n' for token in prompt))
        with (directory/'stdout.log').open('w') as stdout,(directory/'stderr.log').open('w') as stderr:
            result=subprocess.run([str(binary)],cwd=directory,stdout=stdout,stderr=stderr,timeout=args.timeout)
        stdout=(directory/'stdout.log').read_text();stderr=(directory/'stderr.log').read_text()
        if result.returncode or re.search(r'(?i)\b(fatal|error|warning)\b',stdout+stderr):
            raise RuntimeError('simulation failed or reported diagnostics: '+label)
        obtained_tokens=decode_output(stdout,16,64,prefix='TOKEN')
        obtained_logits=decode_output(stdout,50257,64,prefix='LOGIT')
        wanted=[int(value)&((1<<64)-1) for value in expected.tolist()]
        if obtained_tokens!=tokens or obtained_logits!=wanted:
            raise ValueError('public-interface RTL differs from CPU: '+label)
        records.append({'case':label,'prompt':prompt,'tokens':tokens,'logit_words_equal':50257,
                        'runtime_stdout_sha256':digest(directory/'stdout.log'),'frozen_images':frozen})
    if digest(binary)!=binary_hash:
        raise ValueError('simulator changed during verification')
    _verified_artifact(build,('graph-generation-simulator',),'verilator/simulator')
    checked_rtl,checked_identity=authenticated_rtl(root,args.synthesis_root)
    if checked_rtl!=rtl or checked_identity!=synthesis_identity:
        raise ValueError('selected RTL provenance changed during verification')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'scope':'actual public-interface16token/final-logit comparison; no DDR3/physical acceptance',
        'cases':records,'binary_sha256':binary_hash,'rtl_sha256':digest(rtl),
        'synthesis_identity_sha256':synthesis_identity,'reference_sources':reference_sources},indent=2)+'\n')


if __name__=='__main__': main()
