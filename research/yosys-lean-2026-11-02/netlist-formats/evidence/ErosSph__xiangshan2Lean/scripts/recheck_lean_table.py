"""Recheck an unchanged circuit packet with an updated pure Lean evaluator.

Data hashes and the original manifest are retained. No RTL or FunctionalIR
frontend is rerun, and no previous compiled model is overwritten.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil

from common import ROOT, lean_executable, run_logged
from yosys_hierarchy import digest


def write(path, value):
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def prepare(source, output):
    original_path = source / 'manifest.json'
    original = json.loads(original_path.read_text(encoding='utf-8'))
    if original.get('lean_backend') != 'table' or not original['lean_checked']:
        raise ValueError('Expected an already checked Lean table model')
    if output.exists():
        raise ValueError('Keep the original model and use a new output directory')
    output.mkdir(parents=True)
    assets = {original['lean'], original['ir'], original['interface'],
              original['table']['binary'], original['table']['hexadecimal']}
    for name in assets:
        path = (source / name).resolve()
        if not path.is_relative_to(source) or digest(path) != original['artifact_sha256'][path.relative_to(source).as_posix()]:
            raise ValueError('Original model artifact changed: ' + name)
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    runtime = output / original['table']['runtime']
    runtime.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'lean/Chisel2Lean/Functional.lean', runtime)
    counts = json.loads((source / 'functional-import-summary.json').read_text(encoding='utf-8'))
    if counts['nodes'] != original['nodes']:
        raise ValueError('Original typed import count differs from manifest')
    write(output / 'upstream-manifest.json', original)
    prepared = {'status': 'PREPARED', 'source_manifest': str(original_path), 'source_manifest_sha256': digest(original_path),
                'runtime_sha256': digest(runtime), 'output': str(output),
                'counts': {key: counts[key] for key in ('nodes', 'inputs', 'states', 'outputs')},
                'refresher_sha256': digest(Path(__file__))}
    write(output / 'recheck.json', prepared)
    return prepared


def check(output):
    prepared = json.loads((output / 'recheck.json').read_text(encoding='utf-8'))
    original = json.loads((output / 'upstream-manifest.json').read_text(encoding='utf-8'))
    source_manifest = Path(prepared['source_manifest'])
    if digest(source_manifest) != prepared['source_manifest_sha256'] or original != json.loads(source_manifest.read_text(encoding='utf-8')):
        raise ValueError('Original model manifest changed')
    for name in {original['lean'], original['ir'], original['interface'],
                 original['table']['binary'], original['table']['hexadecimal']}:
        artifact = (output / name).resolve()
        if not artifact.is_relative_to(output) or digest(artifact) != original['artifact_sha256'][artifact.relative_to(output).as_posix()]:
            raise ValueError('Prepared circuit artifact differs from the checked original: ' + name)
    runtime = output / original['table']['runtime']
    lean = output / original['lean']
    if digest(runtime) != prepared['runtime_sha256']:
        raise ValueError('Prepared evaluator changed')
    env = os.environ.copy()
    env['LEAN_PATH'] = str(output)
    for label, path in [('lean-runtime-check', runtime), ('lean-check', lean)]:
        code = run_logged([lean_executable(), '-o', str(path.with_suffix('.olean')), str(path)],
                          output / 'evidence' / label, cwd=output, env=env, echo=False)
        if code:
            prepared.update(status='FAIL', failed_stage=label, exit_code=code)
            write(output / 'recheck.json', prepared)
            raise RuntimeError('Lean table recheck failed: ' + label)
    prefix = 'C2L_TABLE_CHECK '
    audits = [json.loads(line[len(prefix):]) for line in (output / 'evidence/lean-check/stdout.log').read_text(encoding='utf-8').splitlines() if line.startswith(prefix)]
    if audits != [prepared['counts']]:
        raise ValueError('Lean did not validate the complete unchanged packet')
    prepared.update(status='PASS', lean_compilation='PASS', complete_data_validation='PASS',
                    check_refresher_sha256=digest(Path(__file__)))
    manifest = copy.deepcopy(original)
    manifest.update(table_runtime_sha256=prepared['runtime_sha256'], evidence=str(output / 'evidence'),
                    source_table_manifest={'path': prepared['source_manifest'], 'sha256': prepared['source_manifest_sha256']},
                    table_runtime_recheck=prepared, lean_checked=True)
    assets = [manifest['lean'], manifest['ir'], manifest['interface'], manifest['table']['runtime'],
              manifest['table']['binary'], manifest['table']['hexadecimal']]
    manifest['artifact_sha256'] = {(output / name).relative_to(output).as_posix(): digest(output / name) for name in assets}
    write(output / 'manifest.json', manifest)
    write(output / 'recheck.json', prepared)
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if args.prepared:
        output = args.prepared.resolve()
        result = json.loads((output / 'recheck.json').read_text(encoding='utf-8'))
    else:
        if not args.source or not args.out:
            parser.error('Supply source and a new output directory, or a prepared directory')
        output = args.out.resolve()
        result = prepare(args.source.resolve(), output)
    if not args.prepare_only:
        result = check(output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
