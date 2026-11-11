#!/usr/bin/env python3
"""Try whole generated SV -> Yosys FunctionalIR -> xiangshan2Lean source.

Research spike only: normalization, Python translation and generated Lean are
not verified by this script. Full intermediates stay under /tmp. This runner
does not alter the production compiler or its artifacts.
"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
import traceback


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (6 * 1024 ** 3, 6 * 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CPU, (180, 185))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yosys', type=Path, required=True)
    parser.add_argument('--importer-source', type=Path, required=True)
    parser.add_argument('--rtl', type=Path, default=Path('result-model-rtl'))
    parser.add_argument('--sv', type=Path, nargs='+', help='Optional fragment files instead of whole RTL')
    parser.add_argument('--top', default='model_top')
    parser.add_argument('--out', type=Path, default=Path(__file__).parent / 'hardware-results')
    parser.add_argument('--work', type=Path, default=Path('/tmp/llm2fpga-yosys-functional-probe'))
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    inputs = args.sv or [args.rtl / name for name in ['core.sv', 'model.sv', 'memory.sv']]
    report = {'yosys': str(args.yosys), 'yosys_version': subprocess.check_output(
        [str(args.yosys), '-V'], text=True).strip(),
        'source_inputs': [{'path': str(p.resolve()), 'bytes': p.stat().st_size,
                           'sha256': sha(p)} for p in inputs],
        'importer_commit': 'a1299b564831e571b9b80f816219ae06c91db342',
        'clock_contract': 'Explicit global sampling after clk2fflogic; two-state completion',
        'limits': {'address_space_gib': 6, 'cpu_seconds': 180, 'wall_seconds': 210},
        'lean_compiler_run': False, 'equivalence_proved': False, 'stages': {}}
    script = '\n'.join([
        'read_verilog -sv ' + ' '.join(str(p.resolve()) for p in inputs),
        'hierarchy -check -top ' + args.top, 'proc', 'flatten', 'memory_collect',
        'opt_clean', 'write_json ' + str(args.work / 'before.json'),
        'clk2fflogic -nolower -nopeepopt', 'memory_nordff', 'opt_clean',
        'setundef -undriven -anyseq', 'check -assert',
        'write_json ' + str(args.work / 'unconstrained.json')]) + '\n'
    script_path = args.out / 'export.ys'
    script_path.write_text(script)

    def save():
        (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

    save()
    started = time.monotonic()
    with (args.out / 'yosys.log').open('w') as log:
        try:
            result = subprocess.run([str(args.yosys), '-Q', '-T', '-s', str(script_path)],
                                    stdout=log, stderr=subprocess.STDOUT, preexec_fn=limits,
                                    timeout=210)
            report['stages']['export'] = {'returncode': result.returncode,
                'status': 'completed' if result.returncode == 0 else 'failed'}
        except subprocess.TimeoutExpired:
            report['stages']['export'] = {'status': 'resource_limit', 'reason': 'wall_timeout'}
    report['stages']['export']['seconds'] = time.monotonic() - started
    if report['stages']['export']['status'] == 'completed':
        try:
            sys.path.insert(0, str(args.importer_source / 'scripts'))
            from functional_ir import expose_arbitrary, preflight
            before = json.loads((args.work / 'before.json').read_text())
            report['preflight'] = preflight(before['modules'][args.top], 'clk', clock_mode='global')
            exposed = json.loads((args.work / 'unconstrained.json').read_text())
            report['arbitrary_inputs'] = expose_arbitrary(exposed['modules'][args.top])
            (args.work / 'exposed.json').write_text(json.dumps(exposed))
            functional_script = '\n'.join([
                'read_json ' + str(args.work / 'exposed.json'), 'check -assert',
                'write_json ' + str(args.work / 'lowered.json'), 'select ' + args.top,
                'write_functional_smt2 ' + str(args.work / 'functional.smt2')]) + '\n'
            functional_path = args.out / 'functional-export.ys'
            functional_path.write_text(functional_script)
            with (args.out / 'yosys.log').open('a') as log:
                result = subprocess.run([str(args.yosys), '-Q', '-T', '-s', str(functional_path)],
                    stdout=log, stderr=subprocess.STDOUT, preexec_fn=limits, timeout=210)
            report['stages']['functional_export'] = {'returncode': result.returncode,
                'status': 'completed' if result.returncode == 0 else 'failed'}
        except Exception as error:
            report['stages']['functional_export'] = {'status': 'failed',
                'error_type': type(error).__name__, 'error': str(error)}
            (args.out / 'export.traceback.txt').write_text(traceback.format_exc())
    log_path = args.out / 'yosys.log'
    log_text = log_path.read_text()
    (args.out / 'yosys.log.tail.txt').write_text('\n'.join(log_text.splitlines()[-60:]) + '\n')
    if 'std::bad_alloc' in log_text:
        report['stages']['export']['diagnostic'] = 'std::bad_alloc during PROC_DLATCH under configured address-space cap'
    with log_path.open('rb') as source, gzip.open(args.out / 'yosys.log.gz', 'wb') as target:
        shutil.copyfileobj(source, target)
    log_path.unlink()
    save()
    if report['stages'].get('functional_export', {}).get('status') == 'completed':
        started = time.monotonic()
        try:
            sys.path.insert(0, str(args.importer_source / 'scripts'))
            from functional_ir import emit_lean, import_functional_file
            lowered = json.loads((args.work / 'lowered.json').read_text())
            top = lowered['modules'][args.top]
            report['cells'] = len(top['cells'])
            report['ports'] = len(top['ports'])
            ir = import_functional_file(args.work / 'functional.smt2', top,
                                        'TinyStoriesProbe', None, compact_nodes=True)
            lean_path = args.work / 'TinyStoriesProbe.lean'
            lean_path.write_text(emit_lean(ir))
            report['stages']['python_import'] = {'status': 'completed'}
            report['imported'] = {'inputs': len(ir['inputs']), 'states': len(ir['states']),
                'outputs': len(ir['outputs']), 'nodes': len(ir['nodes']),
                'memory_states': sum(field['sort']['kind'] == 'memory' for field in ir['states'])}
        except Exception as error:
            report['stages']['python_import'] = {'status': 'failed',
                'error_type': type(error).__name__, 'error': str(error)}
            (args.out / 'import.traceback.txt').write_text(traceback.format_exc())
        report['stages']['python_import']['seconds'] = time.monotonic() - started
    for filename in ['lowered.json', 'functional.smt2', 'TinyStoriesProbe.lean']:
        path = args.work / filename
        if path.exists():
            report.setdefault('intermediates', []).append({'path': str(path),
                'bytes': path.stat().st_size, 'sha256': sha(path)})
    save()
    print(json.dumps(report, indent=2))
