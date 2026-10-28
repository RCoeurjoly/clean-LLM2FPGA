#!/usr/bin/env python3
"""Run mandatory graph numerical checks; never grant physical or code approval."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

D = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(D), str(D / 'scripts/comparison')]

PATH_KEYS = frozenset(('snapshot', 'package', 'reference_source', 'numerical_contract',
    'calyx_library', 'transport', 'loaded_build', 'block_root', 'block_build',
    'block_sv_stage', 'python', 'verilator', 'mlir_runner', 'task2_source'))


def overlaps(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def authenticate_kernel_stages(work, evidence):
    """Match each reported compiler receipt to its fresh on-disk native stage."""
    from compiler.stages import verify_stage_receipt
    dependencies = {}
    for kernel in evidence['kernels']:
        name = kernel['kernel']
        if name not in ('task2', 'gemv', 'argmax'):
            raise ValueError('unknown kernel stage owner')
        root = Path(work) / name
        specifications = tuple((root / 'compile').rglob('spec.json')) + (root / 'inventory/spec.json',)
        observed = []
        for specification in specifications:
            dependencies.update(verify_stage_receipt(specification.parent))
            observed.append(json.loads((specification.parent / 'success.json').read_text()))
        reported = kernel['stages']
        if not reported or len(observed) != len(reported):
            raise ValueError('missing or unexpected kernel stage evidence')
        for receipt in reported:
            if observed.count(receipt) != 1:
                raise ValueError('kernel stage receipt differs from native execution')
    return dependencies


def validate_check_evidence(name, evidence, core_identity):
    """Enforce comparison coverage in addition to successful child execution."""
    def require(condition):
        if not condition:
            raise ValueError("incomplete comparison evidence: " + name)

    try:
        if name == 'kernels':
            kernels = evidence['kernels']
            require(len(kernels) == 3 and {k['kernel'] for k in kernels} == {'task2', 'gemv', 'argmax'})
            for kernel in kernels:
                require(len(kernel['cases']) == (5 if kernel['kernel'] == 'argmax' else 3))
                require(kernel['stages'])
                require(all(c['rtl'] == c['pytorch'] for c in kernel['cases']))
        elif name == 'rounding':
            cases = evidence['cases']
            require(len(cases) == 64 and len({c['case'] for c in cases}) == 64)
            require(all(c['eager_and_compiled_equal'] is True and c['actual'] == c['expected'] for c in cases))
        elif name == 'contexts':
            cases = evidence['comparisons']
            require(len(cases) == 32)
            require({(c['case'], c['context_length']) for c in cases} == {
                (label, length) for label in ('sequential', 'alternate') for length in range(4, 20)})
            require(all(c['logit_words_equal'] == 50257 for c in cases))
        elif name == 'block':
            cases = evidence['comparisons']
            require(len(cases) == 3 and {c['case'] for c in cases} == {'zeros', 'embedding-1', 'embedding-2'})
            require(all(c['words_equal'] == 256 for c in cases))
        elif name == 'loaded':
            cases = evidence['cases']
            require(evidence['core_identity'] == core_identity)
            require(len(cases) == 2 and {c['case'] for c in cases} == {'sequential', 'alternate'})
            require(all(len(c['tokens']) == 16 and c['logit_words_equal'] == 50257 for c in cases))
        else:
            raise ValueError('unknown mandatory check: ' + name)
    except (AssertionError, KeyError, TypeError) as error:
        raise ValueError('incomplete comparison evidence: ' + name) from error


def verify_graph_pipeline(core, verification_inputs, output_dir):
    core, inputs, output = map(Path, (core, verification_inputs, output_dir))
    if output.is_symlink():
        raise ValueError('verification output must not be a symlink')
    core, inputs, output = core.resolve(), inputs.resolve(), output.resolve()
    protected = (core, inputs, D / 'compiler', D / 'scripts', D / 'rtl', D / 'nix',
                 D / 'TinyStories', D / 'model_inputs', D / 'model_packages',
                 D / 'calyx', D / 'tests', D / 'flake.nix', D / 'flake.lock')
    if any(overlaps(output, path) for path in protected):
        raise ValueError('verification output overlaps protected input')
    report = output / 'verification.json'
    try:
        document = json.loads(inputs.read_text())
    except (OSError, ValueError):
        report.unlink(missing_ok=True)
        raise
    # Protect declared paths even when the remaining schema is malformed.
    if isinstance(document, dict):
        for name in PATH_KEYS:
            value = document.get(name)
            if isinstance(value, str) and Path(value).is_absolute():
                if overlaps(output, Path(value).resolve()):
                    raise ValueError('verification output overlaps manifest input')
    report.unlink(missing_ok=True)
    if (not isinstance(document, dict) or set(document) != PATH_KEYS | {'schema_version'} or
            type(document['schema_version']) is not int or document['schema_version'] != 1):
        report.unlink(missing_ok=True)
        raise ValueError('complete fixed verification manifest required; no skip flags')
    paths = {}
    for name in sorted(PATH_KEYS):
        value = document[name]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('manifest paths must be absolute strings: ' + name)
        paths[name] = Path(value).resolve()
    if any(overlaps(output, path) for path in paths.values()):
        raise ValueError('verification output overlaps manifest input')
    # Invalidate only our acceptance document after protecting every declared input.
    report.unlink(missing_ok=True)
    for path in paths.values():
        path.resolve(strict=True)
    if output.exists() and any(output.iterdir()):
        raise ValueError('composed verification requires a fresh output directory')
    from compiler.stages import digest, _identity, _write_json
    from compiler.build_core import verify_core_receipt
    from scripts.pipeline.export_graph_ddr_transport import read_profile
    from graph_loaded_simulator import authenticate_loaded_simulator
    from verify_graph_block_rtl import authenticate_block_simulator
    profile = read_profile(core, paths['numerical_contract'], paths['calyx_library'])
    compiled = verify_core_receipt(core, profile)
    _, dependencies = authenticate_loaded_simulator(paths['loaded_build'], core,
                                                    profile, paths['transport'])
    block_binary = paths['block_build'] / 'emission/verilator/simulator'
    dependencies.update(authenticate_block_simulator(paths['block_root'],
        paths['block_build'], paths['block_sv_stage'], block_binary))
    dependencies.update(compiled['dependency_hashes'])
    dependencies[str(inputs)] = digest(inputs)
    for name in ('python', 'verilator', 'mlir_runner', 'task2_source'):
        dependencies[str(paths[name])] = digest(paths[name])
    dependencies[str(Path(__file__).resolve())] = digest(Path(__file__))
    output.mkdir(parents=True)
    results = []

    def run(name, script, arguments):
        directory = output / name
        directory.mkdir()
        source = D / 'scripts/comparison' / script
        dependencies[str(source)] = digest(source)
        receipt = directory / 'result.json'
        argv = [str(paths['python']), str(source)]
        for key, value in arguments.items():
            argv.extend(['--' + key, str(value)])
        with (directory / 'stdout.log').open('w') as stdout, (directory / 'stderr.log').open('w') as stderr:
            try:
                process = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=28800)
            except subprocess.TimeoutExpired:
                _write_json(directory / 'process.json', {'argv': argv, 'exit_code': None,
                    'completed': False, 'timeout_seconds': 28800})
                raise
        _write_json(directory / 'process.json', {'argv': argv, 'exit_code': process.returncode})
        if process.returncode != 0 or not receipt.is_file():
            raise ValueError('mandatory composed check failed: ' + name)
        # Require parseable fresh evidence, not an arbitrary cached acceptance file.
        evidence = json.loads(receipt.read_text())
        validate_check_evidence(name, evidence, compiled['identity_sha256'])
        if name == 'kernels':
            dependencies.update(authenticate_kernel_stages(directory / 'work', evidence))
        dependencies[str(receipt)] = digest(receipt)
        for log in (directory / 'stdout.log', directory / 'stderr.log', directory / 'process.json'):
            dependencies[str(log)] = digest(log)
        results.append({'check': name, 'receipt': str(receipt), 'sha256': digest(receipt)})

    tools = {name: profile.tools[name].executable for name in
             ('torch', 'circt', 'memory', 'translate', 'calyx')}
    run('kernels', 'verify_graph_kernel_rtl.py', {**tools, 'verilator': paths['verilator'],
        'calyx-library': paths['calyx_library'], 'task2-source': paths['task2_source'],
        'work-dir': output / 'kernels/work', 'receipt': output / 'kernels/result.json'})
    run('rounding', 'verify_graph_rounding.py', {'tool': tools['torch'],
        'runner': paths['mlir_runner'], 'work-dir': output / 'rounding/work',
        'receipt': output / 'rounding/result.json'})
    reference = {name.replace('_', '-'): paths[name] for name in
                 ('snapshot', 'package', 'reference_source')}
    run('contexts', 'verify_graph_generation_steps.py', {**reference,
        'captures': core / 'captures', 'output': output / 'contexts/result.json'})
    run('block', 'verify_graph_block_rtl.py', {**reference, 'root': paths['block_root'],
        'binary': block_binary, 'build-dir': paths['block_build'],
        'sv-stage': paths['block_sv_stage'], 'work-dir': output / 'block/work',
        'timeout': 7200, 'output': output / 'block/result.json'})
    run('loaded', 'verify_graph_loaded_generation.py', {**reference, 'core': core,
        'transport': paths['transport'], 'build': paths['loaded_build'],
        'numerical-contract': paths['numerical_contract'], 'calyx-library': paths['calyx_library'],
        'work': output / 'loaded/work', 'report': output / 'loaded/result.json'})
    verify_core_receipt(core, profile)
    authenticate_loaded_simulator(paths['loaded_build'], core, profile, paths['transport'])
    authenticate_block_simulator(paths['block_root'], paths['block_build'],
                                paths['block_sv_stage'], block_binary)
    if any(digest(Path(path)) != expected for path, expected in dependencies.items()):
        raise ValueError('composed verification dependency changed during execution')
    result = {'schema_version': 1, 'core_identity': compiled['identity_sha256'],
        'checks': results, 'dependency_hashes': dependencies,
        'scope': 'kernel, rounding, context, block and behavioral-DDR numerical replay',
        'physical_acceptance': False, 'manual_review_ready': False}
    result['identity_sha256'] = _identity(result)
    _write_json(report, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('core', 'verification-inputs', 'output-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    verify_graph_pipeline(args.core, args.verification_inputs, args.output_dir)
