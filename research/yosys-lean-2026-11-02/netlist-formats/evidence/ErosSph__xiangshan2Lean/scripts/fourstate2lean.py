"""Translate the strict four-state RTL subset through the unchanged two-state backend."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from common import ROOT, lean_executable, run_logged
from fourstate_frontend import parse
from fourstate_encode import encode
from yosys2lean import translate_yosys


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def fresh_directory(path):
    path = Path(path).resolve()
    bases = [(ROOT / 'generated/fourstate').resolve(), (ROOT / 'experiments/fourstate').resolve()]
    if not any(path.is_relative_to(base) and path != base for base in bases):
        raise ValueError('Use an independent child of generated/fourstate or experiments/fourstate')
    if path.exists() and any(path.iterdir()):
        raise ValueError('Refusing to overwrite nonempty directory: ' + str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path


def provenance(output):
    folder = output / 'source-snapshot'
    folder.mkdir()
    files = sorted(set((ROOT / 'scripts').rglob('*fourstate*.py')) |
                   {p for p in ROOT.glob('verification/fourstate/**/*') if p.is_file()} |
                   {ROOT / name for name in ('README.md', 'AGENTS.md', 'TASKS.md', 'docs/fourstate-frontend.md')})
    hashes = {}
    for src in files:
        relative = src.relative_to(ROOT)
        dest = folder / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        hashes[relative.as_posix()] = digest(src)
    for args, name in [(['rev-parse', 'HEAD'], 'head.txt'),
                       (['status', '--porcelain=v1', '--untracked-files=all'], 'status.txt'),
                       (['diff', '--binary', 'HEAD', '--', 'scripts', 'verification/fourstate', 'docs/fourstate-frontend.md', 'README.md', 'AGENTS.md', 'TASKS.md'], 'working-tree.diff')]:
        (folder / name).write_bytes(subprocess.check_output(['git', *args], cwd=ROOT))
    return hashes


def check_interface(ir, mapping):
    if ir['states'] or ir['clock'] is not None or ir.get('effects'):
        raise ValueError('Four-state combinational model must have no state, clock or effects')
    for kind, direction in [('inputs', 'input'), ('outputs', 'output')]:
        expected = {p[key]: p['width'] for p in mapping['ports'] if p['direction'] == direction and p.get('role') != 'clock' for key in ('tag', 'value')}
        fields = ('current_tag', 'current_value') if direction == 'input' else ('next_tag', 'next_value')
        expected.update({p[key]: p['width'] for p in mapping.get('states', []) for key in fields})
        if direction == 'output':
            expected.update({p[key]: 1 for p in mapping.get('monitors', []) for key in ('tag','value')})
            if mapping.get('assertions'):
                from fourstate_assertions import FIELDS
                expected.update({a[key]:1 for a in mapping['assertions'] for key in FIELDS})
        actual = {p['name']: p['sort']['width'] for p in ir[kind] if p['sort']['kind'] == 'bits'}
        if actual != expected or len(ir[kind]) != len(expected):
            raise ValueError('Encoded interface mismatch or extra arbitrary inputs: ' + kind)


def prepare_backend(output, top):
    """Purge dead public temporaries before the shared undef-exposure pipeline."""
    folder = output / 'backend'
    folder.mkdir()
    script = folder / 'prepare.ys'
    script.write_text(
        'read_slang --threads 1 --no-proc --no-synthesis-define '
        '--no-default-translate-off-format encoded.sv --top ' + top + '; '
        'hierarchy -check -top ' + top + '; proc -noopt; opt_clean -purge; '
        'check -assert; write_json backend/normalized.json; '
        'write_verilog -noattr backend/design.sv\n', encoding='utf-8')
    code = run_logged([sys.executable, '-B', str(ROOT / 'scripts/yosys_runner.py'),
                       '--native', '-Q', '-T', '-s', 'backend/prepare.ys'],
                      folder / 'command', cwd=output, echo=False)
    write(folder / 'preparation.json', {
        'status': 'PASS' if code == 0 else 'FAIL', 'exit_code': code,
        'input': 'encoded.sv', 'input_sha256': digest(output / 'encoded.sv'),
        'script_sha256': digest(script), 'output': 'backend/design.sv',
        'output_sha256': digest(folder / 'design.sv') if code == 0 else None,
        'scope': 'Official cleanup of unused internal names in a separate encoded RTL copy; ports retained.'})
    if code:
        raise RuntimeError('Four-state backend preparation failed; see ' + str(folder / 'command'))
    return folder / 'design.sv'


def compile_model(output, model_ir):
    """Compile only the four-state model with an explicit Lean thread stack."""
    ir = json.loads(model_ir.read_text(encoding='utf-8'))
    root = output / 'model'
    source = root.joinpath(*ir['namespace'].split('.')).with_suffix('.lean')
    compiled = source.with_suffix('.olean')
    env = os.environ.copy()
    env['LEAN_PATH'] = str(root)
    executable = lean_executable()
    code = run_logged([executable, '-s', '65536', '-o', str(compiled), str(source)],
                      output / 'lean-check' / 'command', cwd=root, env=env, echo=False)
    write(output / 'lean-check' / 'result.json', {
        'status': 'PASS' if code == 0 else 'FAIL', 'exit_code': code, 'stack_kib': 65536, 'threads': 'system default',
        'lean_executable': executable, 'lean_sha256': digest(executable),
        'source': source.relative_to(output).as_posix(), 'source_sha256': digest(source),
        'olean_sha256': digest(compiled) if code == 0 else None,
        'scope': 'Four-state entrypoint compilation; shared backend check=False records emission only.'})
    if code:
        raise RuntimeError('Four-state Lean compilation failed; see ' + str(output / 'lean-check'))


def translate(source, top, output, mode='combinational', clock=None, *, defines=(), include_dirs=(), preprocess_report=None, async_reset=None, bounded_memories=False, bounded_events=False, event_clocks=(), immediate_assertions=False):
    pp = {} if preprocess_report is not None else None
    try:
        ir = parse(Path(source), top, mode, clock, defines=defines, include_dirs=include_dirs, preprocessing=pp, async_reset=async_reset, bounded_memories=bounded_memories, bounded_events=bounded_events, event_clocks=event_clocks, immediate_assertions=immediate_assertions)
    finally:
        if preprocess_report is not None:
            # An independent receipt survives a later semantic rejection.
            with Path(preprocess_report).open('x', encoding='utf-8') as stream:
                json.dump(pp, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
    rtl, mapping = encode(ir)
    output = fresh_directory(output)
    version = ir['format'].rsplit('-', 1)[-1]
    source_assets = []
    if version in ('v4', 'v5'):
        base = Path(source).resolve() if Path(source).is_dir() else Path(source).resolve().parent
        for name in ir['sources']:
            src = Path(name)
            entries = ir.get('preprocessing', {}).get('files', [])
            entry = next((p for p in entries if p['path'] == name), None)
            relative = Path(entry['snapshot']) if entry else Path('original') / src.relative_to(base)
            dest = output / relative
            dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(src.read_bytes())
            if entry and digest(dest) != entry['sha256']:
                raise ValueError('preprocessing source changed before snapshot: ' + name)
            source_assets.append({'path': str(src), 'snapshot': relative.as_posix(), 'sha256': digest(dest)})
        write(output / 'sources.json', {'top': top, 'files': source_assets})
        write(output / 'elaboration.json', ir['elaboration'])
        if 'preprocessing' in ir:
            write(output / 'preprocessing.json', ir['preprocessing'])
    else:
        snapshot = output / 'original.sv'
        snapshot.write_bytes(Path(source).read_bytes())
    (output / 'encoded.sv').write_text(rtl, encoding='utf-8')
    write(output / 'fourstate.ir.json', ir)
    write(output / 'ports.json', mapping)
    sources = provenance(output)
    backend_rtl = prepare_backend(output, mapping['encoded_top'])
    relative_ir = translate_yosys(backend_rtl, output / 'model', mapping['encoded_top'],
                                  namespace='FourState.Model', evidence=output / 'translation',
                                  yosys_engine='native', lean_backend='source', check=False)
    model_ir = output / 'model' / relative_ir
    check_interface(json.loads(model_ir.read_text()), mapping)
    compile_model(output, model_ir)
    artifacts = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
    manifest = {'format': 'chisel2lean-fourstate-manifest-' + version, 'mode': mode, 'status': 'COMPILED',
                'scope': 'Typed four-state subset with source provenance; compilation is not differential verification.',
                'source': str(Path(source).resolve()),
                'mapping': 'ports.json', 'ir': model_ir.relative_to(output).as_posix(),
                'backend_preparation': 'backend/preparation.json', 'lean_check': 'lean-check/result.json',
                'source_snapshot_sha256': sources, 'artifact_sha256': artifacts}
    if version in ('v4', 'v5'):
        manifest.update(sources='sources.json', elaboration='elaboration.json', fourstate_ir='fourstate.ir.json')
        if 'preprocessing' in ir:
            manifest['preprocessing'] = 'preprocessing.json'
    else:
        manifest['source_sha256'] = digest(snapshot)
    if mode == 'sequential':
        manifest.update(clock=clock,
                        scope='Explicit register transition, single binary posedge clock, caller-supplied four-state initial values.')
        if 'events' in ir:
            manifest.update(events=ir['events'], scope='Single direct clock/reset posedge process; explicit four-state sampled event and initial-state contract.')
        if bounded_events:
            manifest['scope'] = 'Bounded latch, direct/inverted/latched-AND clock events; ordered single-input steps; explicit initial states; no memories.'
    write(output / 'fourstate-manifest.json', manifest)
    return output / 'fourstate-manifest.json'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path); p.add_argument('--top', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--mode', choices=['combinational', 'sequential'], default='combinational')
    p.add_argument('--clock')
    p.add_argument('--bounded-events', action='store_true')
    p.add_argument('--immediate-assertions', action='store_true', help='Observe bounded immediate assert/$error diagnostics (requires bounded-events)')
    p.add_argument('--event-clock', action='append', default=[], help='One or two independent top root clocks for bounded events')
    p.add_argument('--bounded-memories', action='store_true', help='Opt in to bounded one-dimensional memory expansion (32 words/array, 1024 total bits, single binary clock)')
    p.add_argument('--async-reset', help='Opt in to one direct posedge asynchronous reset and the bounded four-state event contract')
    p.add_argument('-D', '--define', action='append', default=[], help='Official per-compilation-unit NAME[=value] predefine')
    p.add_argument('-I', '--include-dir', action='append', default=[], type=Path, help='Explicit include search root')
    p.add_argument('--preprocess-report', type=Path, help='New receipt path, also written if a subsequent semantic check rejects')
    args = p.parse_args()
    print(translate(args.source, args.top, args.out, args.mode, args.clock,
                    defines=args.define, include_dirs=args.include_dir, preprocess_report=args.preprocess_report, async_reset=args.async_reset, bounded_memories=args.bounded_memories, bounded_events=args.bounded_events, event_clocks=args.event_clock, immediate_assertions=args.immediate_assertions))


if __name__ == '__main__':
    main()
