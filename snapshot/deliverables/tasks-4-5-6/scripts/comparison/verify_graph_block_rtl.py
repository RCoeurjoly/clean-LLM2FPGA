#!/usr/bin/env python3
"""Verification only: compile the block simulator before invoking this script.

Checks layer zero, context four, all 256 words for three changed runtime inputs.
The root must contain authenticated block bindings and their frozen hex images.
This script and its expected outputs are never compiler dependencies.
"""
from pathlib import Path
import argparse, hashlib, importlib, json, re, subprocess, sys
D = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(D), str(D / 'scripts/comparison')]
from graph_rtl_runtime import install_frozen_images, decode_output
from compiler.stages import digest,verify_stage_receipt


def authenticate_block_simulator(root,build,sv_stage,binary):
    """Require declared build and initialized RTL identities, never existence alone."""
    try:
        build=Path(build).resolve();sv_stage=Path(sv_stage).resolve()
        dependencies=verify_stage_receipt(build)
        compiled=verify_stage_receipt(sv_stage)
        spec=json.loads((build/'spec.json').read_text())
        filename='emission/verilator/simulator'
        if (spec['name']!='graph-block-simulator' or spec['outputs']!=[filename] or
                (build/filename).resolve()!=Path(binary).resolve() or Path(binary).is_symlink()):
            raise ValueError('wrong block build or executable')
        rtl=sv_stage/'kernel.sv'
        if (json.loads((sv_stage/'spec.json').read_text())['name']!='sv' or
                any(spec['input_hashes'].get(path)!=identity for path,identity in compiled.items())):
            raise ValueError('build does not consume the initialized block RTL')
        if (spec['pass_identities'].get('block_root')!=str(Path(root).resolve()) or
                spec['pass_identities'].get('initialized_sv')!=str(rtl)):
            raise ValueError('block build belongs to a different root/design')
        bindings=Path(root).resolve()/'bindings-reused.json'
        if compiled.get(str(bindings))!=digest(bindings):
            raise ValueError('block bindings lack initialized RTL provenance')
        emission=verify_stage_receipt(build/'emission')
        if any(spec['input_hashes'].get(path)!=identity for path,identity in emission.items()):
            raise ValueError('block native emission lacks build provenance')
        native=json.loads((build/'emission/spec.json').read_text())
        if (native['name']!='graph-block-simulator-emission' or str(rtl) not in native['argv'] or
                native['input_hashes'].get(str(rtl))!=digest(rtl) or
                not all(flag in native['argv'] for flag in ('--cc','--exe','--main','--timing','--assert'))):
            raise ValueError('block native command does not compile the selected initialized design')
        dependencies.update(compiled);dependencies.update(emission)
        return dependencies
    except (OSError,KeyError,TypeError,ValueError) as error:
        raise ValueError('authenticated block simulator required: '+str(error)) from error

def main():
    parser = argparse.ArgumentParser(description='Verify one graph-derived context-four production block')
    for name in ('root', 'binary', 'snapshot', 'package', 'reference-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--work-dir', type=Path, help='Separate directory for runtime case files and logs')
    parser.add_argument('--build-dir',type=Path)
    parser.add_argument('--sv-stage',type=Path)
    args = parser.parse_args()
    root, binary = args.root.resolve(), args.binary.resolve()
    inferred=(binary.parents[2] if binary.parts[-3:]==('emission','verilator','simulator')
              else binary.parent.parent)
    build=args.build_dir.resolve() if args.build_dir else (inferred if binary.is_file() else root/'simulator')
    sv_stage=args.sv_stage.resolve() if args.sv_stage else root/'initialized/sv'
    work_dir = args.work_dir.resolve() if args.work_dir else root.parent/(root.name+'-block-runtime')
    if args.output.is_symlink() or work_dir.is_symlink():
        raise ValueError('block verification output must not be a symlink')
    protected=(root,build,sv_stage,args.snapshot.resolve(),args.package.resolve(),args.reference_source.resolve())
    protected+=tuple(D/name for name in ('compiler','scripts','rtl','nix','TinyStories','calyx','model_inputs'))
    output=args.output.resolve()
    if (output==binary or any(output==p or output.is_relative_to(p) for p in protected) or
            any(work_dir==p or work_dir.is_relative_to(p) or p.is_relative_to(work_dir) for p in protected)):
        raise ValueError('block verification output overlaps input/build evidence')
    args.output=output
    args.output.unlink(missing_ok=True)
    if not binary.is_file():
        raise RuntimeError('Compile hardware before creating runtime/reference cases')
    dependencies=authenticate_block_simulator(root,build,sv_stage,binary)
    import numpy as np
    import torch
    from compiler.frontend.package_inputs import load_compile_inputs
    from compiler.frontend.package_model import build_fixed_point_model
    from verify_graph_block_reference import reference_block
    from verify_closure_reference import _pinned_numpy_reference
    inputs = load_compile_inputs(args.snapshot, args.package, D / 'compiler/contracts/tinystories-1m-fixed-point.json')
    model = build_fixed_point_model(inputs)
    cls, identities = _pinned_numpy_reference(args.reference_source)
    reference = cls(args.package)
    math = importlib.import_module(cls.__module__)
    records = []
    for label, tokens in [('zeros', None), ('embedding-1', [1,2,3,4]), ('embedding-2', [50256,0,9,123])]:
        if tokens is None:
            values = np.zeros((4,64), dtype=np.int64)
        else:
            values = reference._embedding('token_embedding.weight', np.asarray(tokens)) + reference._embedding('position_embedding.weight', np.arange(4))
        expected = reference_block(reference, math, values, 0)
        with torch.no_grad():
            actual = model._block(torch.from_numpy(values.copy()).unsqueeze(0), 0).numpy()[0]
        if not np.array_equal(actual, expected):
            raise ValueError('CPU block references disagree: ' + label)
        directory = work_dir / label
        directory.mkdir(parents=True, exist_ok=True)
        frozen = install_frozen_images(json.loads((root / 'bindings-reused.json').read_text())['bindings'], root, directory)
        (directory / 'inputs.hex').write_text(''.join(f'{int(x) & ((1<<64)-1):016x}\n' for x in values.reshape(-1)))
        input_hash=digest(directory/'inputs.hex')
        with (directory / 'stdout.log').open('w') as stdout, (directory / 'stderr.log').open('w') as stderr:
            result = subprocess.run([str(binary)], cwd=directory, stdout=stdout, stderr=stderr, text=True, timeout=args.timeout)
        result.stdout = (directory / 'stdout.log').read_text()
        result.stderr = (directory / 'stderr.log').read_text()
        if '$readmem file not found' in result.stdout:
            raise ValueError('Missing frozen simulation image: ' + label)
        if result.returncode or re.search(r'(?i)\b(fatal|error|warning)\b',result.stdout+result.stderr):
            raise RuntimeError(f'{label}: simulator exited {result.returncode}; see {directory}')
        obtained = decode_output(result.stdout, 256, 64)
        wanted = [int(x) & ((1<<64)-1) for x in expected.reshape(-1)]
        if obtained != wanted:
            mismatch = next(i for i,(x,y) in enumerate(zip(obtained,wanted)) if x != y)
            raise ValueError(f'{label}: output {mismatch}: RTL {obtained[mismatch]:016x}, CPU {wanted[mismatch]:016x}')
        if digest(directory/'inputs.hex')!=input_hash or any(
                digest(directory/name)!=identity for name,identity in frozen.items()):
            raise ValueError('block runtime inputs changed: '+label)
        records.append({'case':label,'words_equal':256,'input_sha256':hashlib.sha256(values.astype('<i8').tobytes()).hexdigest(),'output_sha256':hashlib.sha256(expected.astype('<i8').tobytes()).hexdigest()})
        print(label, 'all 256 RTL output words match both CPU references', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    authenticate_block_simulator(root,build,sv_stage,binary)
    for path,identity in dependencies.items():
        if digest(Path(path))!=identity:
            raise ValueError('block verification input changed')
    args.output.write_text(json.dumps({'comparisons':records,'reference_sources':identities, 'frozen_images':frozen, 'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(), 'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'scope':'One actual production block, context four, three changed runtime cases.'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
