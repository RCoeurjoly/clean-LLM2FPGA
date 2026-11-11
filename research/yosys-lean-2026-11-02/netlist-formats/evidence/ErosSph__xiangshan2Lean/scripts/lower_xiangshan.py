"""Lower a successful full official XiangShan elaboration using pinned native firtool."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import uuid

from common import ROOT, FIRTOOL_VERSION, run_logged
from elaborate_xiangshan import digest, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('elaboration', type=Path, help='Successful summary.json or latest.json')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--initialization', choices=['caller-state', 'simulation'], default='caller-state',
                        help='Use explicit caller state, or retain simulator initialization code')
    args = parser.parse_args()
    report = json.loads(args.elaboration.read_text(encoding='utf-8'))
    if report['status'] != 'PASS':
        raise ValueError('Only complete official generator runs may enter the RTL stage')
    origin = Path(report['evidence'])
    source_name = next(n for n in report['files'] if n.endswith('.fir'))
    source = origin / source_name
    if digest(source) != report['files'][source_name]['sha256']:
        raise ValueError('Official FIRRTL source differs from recorded elaboration')
    attempt = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    folder = (args.out or origin / 'lowering' / attempt).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('RTL output directory must be new or empty, to avoid mixing generated configurations')
    rtl = folder / 'rtl'
    rtl.mkdir(parents=True, exist_ok=True)
    firtool = ROOT / '.tools' / ('firtool-' + FIRTOOL_VERSION) / ('firtool-' + FIRTOOL_VERSION) / 'bin/firtool.exe'
    command = [str(firtool), str(source), '-O=release', '--disable-annotation-unknown',
               '--split-verilog', '--export-module-hierarchy', '--mlir-disable-threading',
               '--default-layer-specialization=enable', '--mlir-print-op-on-diagnostic=false',
               '--verification-flavor=immediate',
               '-o=' + str(rtl)]
    if args.initialization == 'caller-state':
        command.append('--disable-all-randomization')
    print('Native firtool: complete ' + report['config'] + '; evidence=' + str(folder), flush=True)
    code = run_logged(command, folder / 'firtool', cwd=folder, echo=False)
    files = sorted(p for p in rtl.rglob('*') if p.is_file())
    result = {'format': 'xiangshan-full-rtl-v1', 'status': 'PASS' if code == 0 else 'FAIL',
              'config': report['config'], 'cores': report['cores'], 'elaboration': str(args.elaboration.resolve()),
              'source_sha256': report['files'][source_name]['sha256'], 'exit_code': code,
              'firtool_version': FIRTOOL_VERSION, 'firtool_sha256': digest(firtool),
              'verification_layers': 'All enabled by official layer specialization; verification code remains in its owning modules.',
              'verification_flavor': 'Official immediate assertions, supported by the RTL importer; checks are retained.',
              'initialization_profile': args.initialization,
              'initialization_contract': 'Caller supplies unknown state; resets use explicit sampling semantics. Simulator randomization is excluded in caller-state mode. Time-zero equivalence is tested against original/default RTL separately, not assumed from the randomization-disabled RTL.',
              'evidence': str(folder), 'lean_conversion': 'NOT_ATTEMPTED',
              'files': {p.relative_to(rtl).as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files}}
    write(folder / 'summary.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, indent=2))
    print('Emitted files:', len(files))
    if code:
        print('\n'.join((folder / 'firtool/stderr.log').read_text(encoding='utf-8').splitlines()[-20:]))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
