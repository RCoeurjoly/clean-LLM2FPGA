"""Create and verify a relocatable source/model/original-RTL server bundle."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile

from common import ROOT
from yosys_hierarchy import digest

FORMAT = 'xiangshan2lean-server-bundle-v1'
MANIFEST = 'SERVER-BUNDLE.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def portable_name(name):
    return str(name).replace('\\', '/')


def safe_path(root, name):
    if not name or '\\' in name or PureWindowsPath(name).drive or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
        raise ValueError('Invalid bundle-relative path: ' + str(name))
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Bundle path escapes its root: ' + name)
    return path


def model_assets(manifest):
    return sorted({portable_name(manifest[key]) for key in ('lean', 'ir', 'interface')} |
                  {portable_name(manifest['table'][key]) for key in ('runtime', 'binary', 'hexadecimal')})


def verify_bundle(root):
    root = root.resolve()
    manifest = read(root / MANIFEST)
    if manifest.get('format') != FORMAT:
        raise ValueError('Unsupported server bundle')
    total = 0
    for name, expected in manifest['files'].items():
        path = safe_path(root, name)
        if not path.is_file() or path.stat().st_size != expected['bytes'] or digest(path) != expected['sha256']:
            raise ValueError('Bundle file missing or changed: ' + name)
        total += expected['bytes']
    return manifest, {'status': 'PASS', 'files': len(manifest['files']), 'bytes': total,
                      'bundle_manifest_sha256': digest(root / MANIFEST)}


def check_request(path, trace, ports, counts):
    physical = {p['name'] for p in ports['ports'] if p['direction'] == 'input'}
    with path.open(encoding='utf-8') as stream:
        initial = json.loads(stream.readline())['initial']
        if len(initial) != counts['states']:
            raise ValueError('Prepared initial state field count differs from the model')
        del initial
        for index, expected in enumerate(trace):
            line = stream.readline()
            if not line:
                raise ValueError('Prepared request is incomplete at sample ' + str(index))
            inputs = json.loads(line)['inputs']
            if len(inputs) != counts['inputs'] or {name: inputs.get(name) for name in physical} != expected['inputs']:
                raise ValueError('Prepared request differs from physical trace at sample ' + str(index))
            if any(value != 0 for name, value in inputs.items() if name not in physical):
                raise ValueError('Expected the explicitly recorded zero arbitrary-input experiment')
        if stream.read().strip():
            raise ValueError('Prepared request has extra samples')


def package(model, smoke, request, output):
    if output.exists():
        raise ValueError('Keep existing archives and choose a new output path')
    original = read(model / 'manifest.json')
    checked = read(model / 'recheck.json')
    prepared = read(smoke / 'summary.json')
    if not original.get('lean_checked') or original.get('lean_backend') != 'table' or checked['status'] != 'PASS':
        raise ValueError('Expected the completely Lean-checked whole table model')
    if original['source_files_sha256'] != prepared['source_files_sha256']:
        raise ValueError('Whole model and original RTL refer to different sources')
    counts = checked['counts']
    ports, trace = read(smoke / 'ports.json'), read(smoke / 'trace.json')
    if len(trace) != prepared['global_samples']:
        raise ValueError('Prepared physical trace is incomplete')
    check_request(request, trace, ports, counts)
    paths = {}
    def add(path, name, role, expected=None):
        path = path.resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('Only project-owned files may be bundled: ' + str(path))
        safe_path(ROOT, name)
        if name in paths and paths[name][0] != path:
            raise ValueError('Bundle name collision: ' + name)
        value = digest(path)
        if expected is not None and value != expected:
            raise ValueError('Source asset changed: ' + str(path))
        paths[name] = (path, {'sha256': value, 'bytes': path.stat().st_size, 'role': role})

    source_roots = ['src', 'scripts', 'lean', 'project', 'annotations', 'reference/src', 'audit/src',
                    'firrtl_frontend/src', 'testcase/src', 'examples', 'tests', 'verification', 'docs', 'vendor']
    excluded_parts = {'target', '__pycache__', '.git', '.tools', '.cache'}
    excluded_suffixes = {'.pyc', '.olean', '.ilean', '.exe', '.dll', '.o', '.a', '.class'}
    for name in source_roots:
        for path in sorted((ROOT / name).rglob('*')):
            if path.is_file() and not excluded_parts.intersection(path.relative_to(ROOT).parts) and path.suffix not in excluded_suffixes:
                add(path, path.relative_to(ROOT).as_posix(), 'source-or-historical-evidence')
    for name in ['README.md', 'LICENSE', 'NOTICE', 'AGENTS.md', 'TASKS.md', 'build.sbt', 'lean-toolchain', '.gitignore', '.gitattributes', '.scalafmt.conf']:
        if (ROOT / name).is_file():
            add(ROOT / name, name, 'project-source')
    for name in model_assets(original):
        add(model / name, 'model/' + name, 'checked-circuit', original['artifact_sha256'][name])
    add(model / 'manifest.json', 'model/origin-manifest.json', 'original-compilation-provenance')
    add(model / 'recheck.json', 'model/origin-recheck.json', 'original-compilation-provenance')
    for name, expected in prepared['source_files_sha256'].items():
        add(safe_path(smoke / 'rtl', name), 'rtl/' + name, 'original-rtl', expected)
    add(smoke / 'ports.json', 'scenario/ports.json', 'physical-interface')
    add(smoke / 'trace.json', 'scenario/trace.json', 'physical-trace')
    add(request, 'scenario/request.jsonl', 'explicit-initial-state-and-inputs')
    add(smoke / 'summary.json', 'scenario/origin-summary.json', 'historical-status')
    if original['table_runtime_sha256'] != digest(ROOT / 'lean/Chisel2Lean/Functional.lean'):
        raise ValueError('Checked whole model and source evaluator differ')

    fixture = ROOT / 'experiments/yosys-table-pipeline-7.3/model'
    fixture_manifest = read(fixture / 'manifest.json')
    fixture_counts = read(fixture / 'functional-import-summary.json')
    for name in model_assets(fixture_manifest):
        if name != portable_name(fixture_manifest['table']['runtime']):
            add(fixture / name, 'fixture/model/' + name, 'small-checked-circuit', fixture_manifest['artifact_sha256'][name])
    add(fixture / 'manifest.json', 'fixture/model/origin-manifest.json', 'original-fixture-provenance')
    for source, name in [('lean/request.json', 'request.json'), ('lean/result.json', 'expected.json'),
                         ('invalid-lean/request.json', 'invalid-request.json'), ('Observed.sv', 'Observed.sv'),
                         ('reference/result.json', 'rtl-expected.json')]:
        add(ROOT / 'experiments/yosys-exposure-7.3' / source, 'fixture/' + name, 'rtl-checked-smoke-fixture')
    manifest = {'format': FORMAT, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'config': 'DefaultConfig', 'cores': 1, 'top': 'XSTop',
        'upstream_commit': '997cd5b35cfbd26586674aa553d0f04b27914cb2',
        'lean_version': '4.19.0', 'reference_verilator_version': '5.048',
        'model': {'directory': 'model', 'counts': counts},
        'fixture': {'directory': 'fixture/model', 'counts': {key: fixture_counts[key] for key in ('nodes', 'inputs', 'states', 'outputs')}},
        'scenario': {'global_samples': len(trace), 'main_clock_cycles': prepared['cycles'],
            'scenario': prepared['scenario'], 'choices': prepared['choices'],
            'initialization_profile': prepared['initialization_profile']},
        'acceptance_at_packaging': {'windows_lean_compilation': 'PASS', 'complete_data_validation': 'PASS',
            'whole_execution': 'NOT_COMPLETED', 'whole_rtl_differential': 'NOT_RUN',
            'instruction_execution': 'NOT_TESTED', 'universal_equivalence': 'NOT_PROVED'},
        'files': {name: value[1] for name, value in sorted(paths.items())}}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.partial')
    top = 'xiangshan2lean-server'
    with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for name, (source, _) in sorted(paths.items()):
            archive.write(source, top + '/' + name)
        archive.writestr(top + '/' + MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
    with zipfile.ZipFile(temporary) as archive:
        for name, info in manifest['files'].items():
            checksum = hashlib.sha256()
            with archive.open(top + '/' + name) as stream:
                for data in iter(lambda: stream.read(4*1024*1024), b''):
                    checksum.update(data)
            if checksum.hexdigest() != info['sha256']:
                raise ValueError('Archive verification failed: ' + name)
    temporary.replace(output)
    checksum = digest(output)
    output.with_suffix(output.suffix + '.sha256').write_bytes((checksum + '  ' + output.name + '\n').encode('utf-8'))
    report = {'status': 'PASS', 'archive': str(output), 'bytes': output.stat().st_size,
              'sha256': checksum, 'files': len(paths), 'uncompressed_bytes': sum(p[1]['bytes'] for p in paths.values()),
              'scope': 'Verified transfer artifact only; does not claim server execution or whole-chip acceptance.'}
    write(output.with_suffix('.json'), report)
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path)
    parser.add_argument('--smoke', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_bundle(args.verify)[1], indent=2))
    elif not all((args.model, args.smoke, args.request, args.out)):
        parser.error('Supply model, smoke, request and a new output archive, or --verify')
    else:
        package(args.model.resolve(), args.smoke.resolve(), args.request.resolve(), args.out.resolve())


if __name__ == '__main__':
    raise SystemExit(main())
