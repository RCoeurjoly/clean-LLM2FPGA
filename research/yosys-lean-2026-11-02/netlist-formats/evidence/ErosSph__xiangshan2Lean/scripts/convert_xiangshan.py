"""Package complete official XiangShan RTL and attempt strict whole-top Lean conversion."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import traceback
import uuid

from common import ROOT
from yosys2lean import digest, translate_yosys, write


def blocking_diagnostics(folder):
    diagnostics = []
    for path in sorted((folder / 'translation').glob('*/*/*.log')):
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                line = line.strip()
                if any(term in line for term in (': ERROR:', 'ERROR:', 'wasm trap:', 'std::bad_alloc',
                    'out of memory', 'Assertion failed', 'terminate called', 'what():')) and line not in diagnostics:
                    diagnostics.append(line)
                    if len(diagnostics) == 20:
                        return diagnostics
    return diagnostics


def package_rtl(lowering, destination):
    report = json.loads(lowering.read_text(encoding='utf-8'))
    if report['status'] != 'PASS':
        raise ValueError('Whole-top conversion requires a complete successful native firtool run')
    elaboration = json.loads(Path(report['elaboration']).read_text(encoding='utf-8'))
    if elaboration['status'] != 'PASS' or elaboration['hardware_source_modified']:
        raise ValueError('Expected successful original official source elaboration')
    destination.mkdir(parents=True)
    sources = {}
    def copy(source, relative, expected, role):
        if digest(source) != expected:
            raise ValueError('Recorded original source hash changed: ' + str(source))
        target = destination / relative
        if target.exists() and digest(target) != expected:
            raise ValueError('Generated resource name collision: ' + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        sources[relative] = {'source': str(source), 'sha256': expected, 'role': role}
    rtl = Path(report['evidence']) / 'rtl'
    for name, info in report['files'].items():
        if Path(name).suffix in {'.sv', '.v', '.vh', '.svh'}:
            copy(rtl / name, name, info['sha256'], 'native-firtool')
    # Original TopMain writes DifftestMacros.v after ChiselStage, outside target-dir.
    # Retain this generated header so source guards are evaluated as authored.
    for name, info in elaboration['files'].items():
        if name.startswith('build/generated-src/') and Path(name).suffix in {'.sv', '.v', '.vh', '.svh'}:
            copy(Path(elaboration['evidence']) / name, Path(name).name, info['sha256'], 'official-generator-resource')
    if not sources:
        raise ValueError('Successful native lowering report contains no RTL')
    return report, {'sources': sources, 'additional_preprocessor_defines': [],
                    'hardware_sources_modified': False,
                    'scope': 'Every emitted RTL unit plus original generated headers; no rewritten hardware or implicit blackbox replacement.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lowering', type=Path, help='Successful lower_xiangshan.py summary.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'generated/xiangshan-whole')
    parser.add_argument('--yosys-engine', choices=['wasm', 'native'], default='native')
    parser.add_argument('--hierarchy-checkpoint', type=Path)
    parser.add_argument('--flat-checkpoint', type=Path)
    parser.add_argument('--normalized-checkpoint', type=Path)
    parser.add_argument('--functional-checkpoint', type=Path)
    parser.add_argument('--lean-backend', choices=['auto', 'source', 'table'], default='auto')
    parser.add_argument('--evidence', type=Path, default=ROOT / 'experiments/xiangshan-whole-lean-7.3')
    parser.add_argument('--top', default='XSTop')
    parser.add_argument('--package-only', action='store_true')
    args = parser.parse_args()
    attempt = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    folder = args.evidence.resolve() / attempt
    folder.mkdir(parents=True)
    lowering, package = package_rtl(args.lowering.resolve(), folder / 'rtl')
    write(folder / 'source-package.json', package)
    result = {'format': 'xiangshan-whole-lean-v1', 'config': lowering['config'], 'cores': lowering['cores'],
              'yosys_engine': args.yosys_engine,
              'initialization_profile': lowering.get('initialization_profile', 'simulation'),
              'stdio_compatibility': 'Fixed standard-stream $fwrite preserved as observations; original RTL retained.',
              'top': args.top, 'source_package': str(folder / 'source-package.json'),
              'lowering': str(args.lowering.resolve()), 'status': 'PACKAGED',
              'rtl_units_and_headers': len(package['sources']), 'lean_compilation': 'NOT_ATTEMPTED',
              'top_level_differential': 'NOT_RUN', 'universal_equivalence': 'NOT_PROVED',
              'evidence': str(folder)}
    code = 0
    if not args.package_only:
        result.update(status='RUNNING', lean_compilation='NOT_REACHED')
        write(folder / 'summary.json', result)
        write(args.evidence.resolve() / 'latest.json', result)
        try:
            print('Converting entire ' + args.top + ': ' + str(len(package['sources'])) + ' source files', flush=True)
            relative = translate_yosys(folder / 'rtl', args.out, args.top, 'XiangShan.Whole',
                                       evidence=folder / 'translation', preserve_effects=True,
                                       clock_mode='global', keep_hierarchy=True, stdio_compat=True,
                                       yosys_engine=args.yosys_engine, hierarchy_checkpoint=args.hierarchy_checkpoint,
                                       flat_checkpoint=args.flat_checkpoint,
                                       normalized_checkpoint=args.normalized_checkpoint,
                                       functional_checkpoint=args.functional_checkpoint, lean_backend=args.lean_backend)
            result.update(status='LEAN_COMPILED', lean_compilation='PASS', ir=str((args.out / relative).resolve()))
        except (RuntimeError, ValueError, MemoryError) as error:
            (folder / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            tool_results = {p.parent.name: json.loads(p.read_text(encoding='utf-8'))['exit_code']
                            for p in (folder / 'translation').glob('*/*/result.json')}
            lean_status = ('PASS' if tool_results['lean-check'] == 0 else 'FAIL') if 'lean-check' in tool_results else 'NOT_REACHED'
            diagnostics = blocking_diagnostics(folder)
            reused = [p.parent.name for p in (folder / 'translation').glob('*/*/reused.json')]
            exhausted = isinstance(error, MemoryError) or any('bad_alloc' in line or 'out of memory' in line for line in diagnostics)
            phases = sorted((folder / 'translation').glob('*/progress.json'))
            phase = json.loads(phases[-1].read_text(encoding='utf-8'))['phase'] if phases else 'setup'
            result.update(status='REQUIRES_RESOURCES' if exhausted else 'REQUIRES_SUPPORT', lean_compilation=lean_status,
                          tool_exit_codes=tool_results, diagnostic=str(error) or type(error).__name__ + ' during ' + phase,
                          exception_type=type(error).__name__, phase=phase, exception_trace=str(folder / 'error.txt'),
                          reused_stages=reused,
                          blocking_diagnostics=diagnostics)
            code = 2
    write(folder / 'summary.json', result)
    write(args.evidence.resolve() / 'latest.json', result)
    write(ROOT / 'docs/validation-xiangshan-whole-lean-7.3.json', result)
    print(json.dumps(result, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
