"""Resume the saved fixed XSTop on a host with Lean and Verilator installed."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import uuid

from build_lean_table_runner import build_runner
from common import ROOT, lean_executable, run_logged
from rtl import native_environment, run_emitted_rtl, verilator_command
from server_bundle import MANIFEST, model_assets, portable_name, read, safe_path, verify_bundle, write
from trace_compare import compare_rows
from yosys_hierarchy import digest


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]


def doctor(require_rtl=False):
    if sys.version_info < (3, 11):
        raise RuntimeError('Use Python 3.11 or newer')
    lean = Path(lean_executable())
    version = subprocess.check_output([str(lean), '--version'], text=True).strip()
    if not re.search(r'version 4\.19\.0(?:[, )]|$)', version):
        raise RuntimeError('Expected Lean 4.19.0, got: ' + version)
    leanc = lean.parent / ('leanc.exe' if os.name == 'nt' else 'leanc')
    if not leanc.is_file():
        raise RuntimeError('Install the complete Lean distribution, including leanc')
    report = {'status': 'PASS', 'system': platform.system(), 'machine': platform.machine(),
              'python': sys.version, 'lean': version, 'lean_executable': str(lean),
              'lean_sha256': digest(lean), 'leanc_sha256': digest(leanc)}
    if require_rtl:
        env = native_environment()
        command = verilator_command()
        version = subprocess.check_output([*command, '--version'], env=env, text=True).strip()
        if not re.search(r'Verilator 5\.048\b', version):
            raise RuntimeError('The saved reference uses Verilator 5.048; install that version. Got: ' + version)
        for name in ('make', 'g++'):
            executable = shutil.which(name, path=env.get('PATH'))
            if not executable:
                raise RuntimeError('Reference build requires ' + name)
            report[name] = {'path': executable, 'sha256': digest(Path(executable))}
        report['verilator'] = version
    return report


def stage_model(root, work, bundle, kind):
    source = safe_path(root, bundle[kind]['directory'])
    original = read(source / 'origin-manifest.json')
    target = work / kind
    target.mkdir(parents=True)
    assets = model_assets(original)
    runtime_name = portable_name(original['table']['runtime'])
    for name in assets:
        # Recompile the fixture with the same evaluator as the whole model.
        origin = root / 'lean/Chisel2Lean/Functional.lean' if name == runtime_name else safe_path(source, name)
        output = safe_path(target, name)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, output)
    table = dict(original['table'])
    for key in ('runtime', 'binary', 'hexadecimal'):
        table[key] = portable_name(table[key])
    manifest = {key: original[key] for key in ('format', 'frontend', 'symbolic_parameters', 'namespace',
        'lean_version', 'nodes', 'source_files_sha256', 'semantics', 'clock_mode', 'effect_contract') if key in original}
    manifest.update(lean_backend='table', lean_checked=False,
        lean=portable_name(original['lean']), ir=portable_name(original['ir']),
        interface=portable_name(original['interface']), table=table,
        table_runtime_sha256=digest(target / runtime_name),
        source=str((root / 'rtl').resolve()) if kind == 'model' else str((root / 'fixture/Observed.sv').resolve()),
        source_model_manifest_sha256=digest(source / 'origin-manifest.json'),
        artifact_sha256={name: digest(target / name) for name in assets},
        expected_counts=bundle[kind]['counts'],
        evidence=str(work / 'checks' / kind))
    write(target / 'manifest.json', manifest)


def init_work(root, bundle, requested):
    work = requested.resolve() if requested else root / 'runs' / stamp()
    if any(char.isspace() for char in str(work)):
        raise ValueError('Verilator/GNU Make requires a work path without whitespace; supply --work with such a directory')
    if work.exists():
        raise ValueError('Preserve existing work directories and choose a new one')
    work.mkdir(parents=True)
    write(work / 'session.json', {'format': 'xiangshan2lean-server-session-v1',
        'bundle_manifest_sha256': digest(root / MANIFEST), 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'system': platform.system(), 'machine': platform.machine(), 'status': 'PREPARED'})
    for kind in ('model', 'fixture'):
        stage_model(root, work, bundle, kind)
    return work


def load_work(root, work):
    if work is None:
        raise ValueError('Supply --work with the directory printed by init')
    work = work.resolve()
    session = read(work / 'session.json')
    if session['bundle_manifest_sha256'] != digest(root / MANIFEST):
        raise ValueError('Work directory belongs to a different bundle')
    return work


def compile_model(work, kind, host):
    model = work / kind
    manifest = read(model / 'manifest.json')
    for name, expected in manifest['artifact_sha256'].items():
        if digest(safe_path(model, name)) != expected:
            raise ValueError('Working model asset changed: ' + name)
    if manifest.get('lean_checked') and manifest.get('host_lean_check', {}).get('lean_sha256') == host['lean_sha256']:
        return
    attempt = work / 'checks' / kind / stamp()
    env = os.environ.copy()
    env['LEAN_PATH'] = str(model)
    for label, name in [('runtime', manifest['table']['runtime']), ('circuit', manifest['lean'])]:
        source = model / name
        if run_logged([lean_executable(), '-o', str(source.with_suffix('.olean')), str(source)],
                      attempt / label, cwd=model, env=env, echo=False):
            raise RuntimeError('Lean compilation failed; see ' + str(attempt / label))
    prefix = 'C2L_TABLE_CHECK '
    counts = [json.loads(line[len(prefix):]) for line in (attempt / 'circuit/stdout.log').read_text(encoding='utf-8').splitlines() if line.startswith(prefix)]
    if counts != [manifest['expected_counts']]:
        raise ValueError('Lean did not validate the complete expected packet')
    for name, expected in manifest['artifact_sha256'].items():
        if digest(model / name) != expected:
            raise ValueError('Model asset changed during compilation: ' + name)
    manifest.update(lean_checked=True, host_lean_check=host, evidence=str(attempt))
    write(model / 'manifest.json', manifest)


def build_native(root, work):
    folder = work / 'runner'
    if (folder / 'manifest.json').is_file():
        report = read(folder / 'manifest.json')
        if report['runtime_sha256'] != digest(root / 'lean/Chisel2Lean/Functional.lean'):
            raise ValueError('Existing runner uses a different evaluator')
    else:
        build_runner(root / 'lean/Chisel2Lean/Functional.lean', folder)
    return folder


def native_run(root, work, kind, request, execution, expect_failure=False):
    model, runner = work / kind, work / 'runner'
    manifest, engine = read(model / 'manifest.json'), read(runner / 'manifest.json')
    if not manifest['lean_checked'] or engine['status'] != 'PASS' or manifest['table_runtime_sha256'] != engine['runtime_sha256']:
        raise ValueError('Native execution requires a checked model and matching evaluator')
    executable = Path(engine['executable'])
    if not executable.resolve().is_relative_to(runner.resolve()) or digest(executable) != engine['executable_sha256']:
        raise ValueError('Native runner executable changed')
    tools = Path(lean_executable()).parent
    for name, expected in engine['tools_sha256'].items():
        dependency = (tools / name).resolve()
        if not dependency.is_relative_to(tools.parent.resolve()) or digest(dependency) != expected:
            raise ValueError('Native Lean dependency changed: ' + name)
    binary = safe_path(model, manifest['table']['binary'])
    if digest(binary) != manifest['artifact_sha256'][manifest['table']['binary']]:
        raise ValueError('Circuit packet changed')
    execution.mkdir(parents=True)
    provenance = {'model_manifest_sha256': digest(model / 'manifest.json'), 'binary_sha256': digest(binary),
        'request_sha256': digest(request), 'runner_manifest_sha256': digest(runner / 'manifest.json'),
        'runtime_sha256': engine['runtime_sha256'], 'executable_sha256': engine['executable_sha256']}
    write(execution / 'engine.json', provenance)
    env = os.environ.copy()
    env['PATH'] = str(tools) + os.pathsep + env.get('PATH', '')
    code = run_logged([str(executable), str(binary), str(request)], execution / 'execution', cwd=model, env=env, echo=False)
    if digest(binary) != provenance['binary_sha256'] or digest(request) != provenance['request_sha256']:
        raise ValueError('Packet or request changed during execution')
    if expect_failure:
        message = (execution / 'execution/stderr.log').read_text(encoding='utf-8')
        if code == 0 or 'Assertion or assumption failed' not in message:
            raise ValueError('Invalid property input was not rejected for the expected reason')
        return []
    if code:
        raise RuntimeError('Native Lean failed; see ' + str(execution / 'execution'))
    rows = [json.loads(line) for line in (execution / 'execution/stdout.log').read_text(encoding='utf-8').splitlines() if line.strip()]
    write(execution / 'result.json', rows)
    return rows


def jsonl_request(source, target):
    value = read(source)
    with target.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps({'initial': value['initial']}) + '\n')
        for row in value['trace']:
            stream.write(json.dumps(row) + '\n')


def selftest(root, work, host, with_rtl=False):
    compile_model(work, 'fixture', host)
    build_native(root, work)
    execution = work / 'selftest' / stamp()
    execution.mkdir(parents=True)
    request = execution / 'request.jsonl'
    jsonl_request(root / 'fixture/request.json', request)
    actual = native_run(root, work, 'fixture', request, execution / 'valid')
    if actual != read(root / 'fixture/expected.json'):
        raise AssertionError('Native runner differs from the saved RTL-checked outputs or observations')
    invalid = execution / 'invalid-request.jsonl'
    jsonl_request(root / 'fixture/invalid-request.json', invalid)
    native_run(root, work, 'fixture', invalid, execution / 'invalid', expect_failure=True)
    rtl_check = 'NOT_RUN'
    if with_rtl:
        rtl_directory = execution / 'rtl'
        rtl_directory.mkdir()
        shutil.copy2(root / 'fixture/Observed.sv', rtl_directory / 'Observed.sv')
        trace = [{'reset': row['inputs']['reset'], 'inputs': {key: row['inputs'][key] for key in ('en', 'd')}}
                 for row in read(root / 'fixture/request.json')['trace']]
        metadata = {'top': 'Observed', 'clocked': True, 'assertions': True, 'rtl_sources': ['Observed.sv'], 'jobs': 1, 'output_groups': 0,
            'ports': [{'name': name, 'rtl_name': name, 'width': width, 'direction': 'output' if name == 'q' else 'input'}
                      for name, width in [('clock',1), ('reset',1), ('en',1), ('d',4), ('q',4)]]}
        code, reference = run_emitted_rtl(rtl_directory, metadata, trace, execution / 'reference')
        if code or compare_rows(actual, reference, ['q'], len(trace))['status'] != 'PASS':
            raise AssertionError('Fresh RTL smoke comparison failed')
        rtl_check = 'PASS'
    write(work / 'selftest.json', {'status': 'PASS', 'samples': len(actual), 'outputs_and_observations': 'PASS',
        'invalid_property_rejected': 'PASS', 'fresh_rtl': rtl_check, 'execution': str(execution),
        'scope': 'Small fixture only; whole-chip acceptance is separate.'})


def lean_whole(root, work, bundle, host):
    compile_model(work, 'model', host)
    build_native(root, work)
    execution = work / 'lean' / stamp()
    actual = native_run(root, work, 'model', root / 'scenario/request.jsonl', execution)
    names = [p['name'] for p in read(root / 'scenario/ports.json')['ports'] if p['direction'] == 'output']
    check = compare_rows(actual, actual, names, bundle['scenario']['global_samples'])
    if check['status'] != 'PASS':
        raise ValueError('Whole Lean trace is incomplete or malformed: ' + check['status'])
    write(work / 'lean.json', {'status': 'PASS', 'result': str(execution / 'result.json'),
        'result_sha256': digest(execution / 'result.json'), 'samples': len(actual),
        'bundle_manifest_sha256': digest(root / MANIFEST),
        'scope': 'Complete saved scenario executed in Lean; independent RTL comparison remains separate.'})


def rtl_whole(root, work, bundle, jobs, *, trace=None, execution=None, record=None):
    execution = execution if execution is not None else work / 'rtl' / stamp()
    record = record if record is not None else work / 'rtl.json'
    trace = trace if trace is not None else read(root / 'scenario/trace.json')
    metadata = read(root / 'scenario/ports.json')
    sources = read(root / 'model/origin-manifest.json')['source_files_sha256']
    rtl = work / 'rtl-input'
    rtl.mkdir(exist_ok=True)
    for name, expected in sources.items():
        source, target = safe_path(root / 'rtl', name), safe_path(rtl, name)
        if target.exists():
            if digest(target) != expected:
                raise ValueError('Existing working RTL changed: ' + name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if digest(target) != expected:
            raise ValueError('Working RTL differs from original: ' + name)
    # The first saved row asserts reset with clocks low. Give the simulator an
    # explicit settled all-zero input condition so that assertion is an edge.
    # This changes no RTL, no internal state, and none of the saved 96 samples.
    metadata.update(rtl_include_dirs=[str(rtl)], jobs=jobs, output_groups=0,
        initial_inputs={p['name']: 0 for p in metadata['ports'] if p['direction'] == 'input'})
    execution.mkdir(parents=True)
    previous = execution / 'previous-working-files'
    for name in ('trace.cpp', 'trace.csv', 'rtl-results.csv', 'obj_dir/VXSTop'):
        source = rtl / name
        if source.is_file():
            target = previous / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(Path(__file__), execution / 'server.py')
    write(execution / 'metadata.json', metadata)
    write(execution / 'physical-trace.json', trace)
    code, rows = run_emitted_rtl(rtl, metadata, trace, execution)
    if code or len(rows) != len(trace):
        raise RuntimeError('Whole original RTL execution failed; see ' + str(execution))
    for name, expected in sources.items():
        if digest(rtl / name) != expected:
            raise ValueError('Original working RTL changed during execution: ' + name)
    write(record, {'status': 'PASS', 'result': str(execution / 'result.json'),
        'result_sha256': digest(execution / 'result.json'), 'samples': len(rows),
        'bundle_manifest_sha256': digest(root / MANIFEST),
        'startup': read(execution / 'startup.json'),
        'driver': read(execution / 'driver.json'),
        'execution': str(execution),
        'physical_trace_sha256': digest(execution / 'physical-trace.json'),
        'scope': 'Complete original RTL scenario executed; Lean comparison remains separate.'})
    return rows


def compare_whole(root, work, bundle):
    if not all((work / name).is_file() for name in ('lean.json', 'rtl.json')):
        raise ValueError('Run the complete Lean and original RTL phases before comparison')
    records = [read(work / name) for name in ('lean.json', 'rtl.json')]
    traces = []
    for record in records:
        if record['status'] != 'PASS' or record['bundle_manifest_sha256'] != digest(root / MANIFEST) or digest(Path(record['result'])) != record['result_sha256']:
            raise ValueError('Execution result differs from the saved complete run')
        traces.append(read(Path(record['result'])))
    names = [p['name'] for p in read(root / 'scenario/ports.json')['ports'] if p['direction'] == 'output']
    report = compare_rows(*traces, names, bundle['scenario']['global_samples'])
    report.update(config=bundle['config'], cores=bundle['cores'], upstream_commit=bundle['upstream_commit'],
        original_hardware_modified=False, scenario=bundle['scenario'],
        bundle_manifest_sha256=digest(root / MANIFEST), instruction_execution='NOT_TESTED', universal_equivalence='NOT_PROVED',
        rtl_startup=records[1].get('startup'), rtl_driver=records[1].get('driver'))
    write(work / 'acceptance.json', report)
    if report['status'] != 'PASS':
        raise AssertionError('Whole-chip comparison failed: ' + report['status'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['doctor', 'verify', 'init', 'selftest', 'compile', 'lean', 'rtl', 'compare', 'all'])
    parser.add_argument('--bundle', type=Path, default=ROOT)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--lean', type=Path)
    parser.add_argument('--verilator', type=Path)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--with-rtl', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('Jobs must be positive')
    if args.lean:
        os.environ['CHISEL2LEAN_LEAN'] = str(args.lean.expanduser().resolve())
    if args.verilator:
        os.environ['CHISEL2LEAN_VERILATOR'] = str(args.verilator.expanduser().resolve())
    if args.action == 'doctor':
        print(json.dumps(doctor(args.with_rtl), indent=2))
        return
    root = args.bundle.resolve()
    bundle, verification = verify_bundle(root)
    if args.action == 'verify':
        print(json.dumps(verification, indent=2))
        return
    host = doctor(args.action in {'rtl', 'all'} or args.with_rtl)
    work = init_work(root, bundle, args.work) if args.action in {'init', 'all'} else load_work(root, args.work)
    write(work / 'host.json', host)
    print('WORK_DIRECTORY=' + str(work), flush=True)
    if args.action in {'selftest', 'all'}:
        selftest(root, work, host, args.with_rtl or args.action == 'all')
        print('PASS small selftest', flush=True)
    if args.action == 'compile':
        compile_model(work, 'model', host)
    if args.action in {'lean', 'all'}:
        lean_whole(root, work, bundle, host)
        print('PASS whole Lean scenario (comparison pending)', flush=True)
    if args.action in {'rtl', 'all'}:
        rtl_whole(root, work, bundle, args.jobs)
        print('PASS whole original RTL scenario (comparison pending)', flush=True)
    if args.action in {'compare', 'all'}:
        # Check that compilation/execution did not change any transferred source.
        verify_bundle(root)
        print(json.dumps(compare_whole(root, work, bundle), indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
