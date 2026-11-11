"""Run the unmodified official XiangShan top-level generator in an isolated directory."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from common import ROOT, java_executable, rtl_environment, run_logged, sbt_command


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def inventory_firrtl(path):
    """Inventory official emitted declarations; this does not interpret FIRRTL semantics."""
    modules, current = {}, None
    with path.open(encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            match = re.match(r'^  (?:(?:public|private) )?(module|extmodule|intmodule) (\S+)\s*:', line)
            if match:
                kind, name = match.groups()
                current = {'name': name, 'kind': kind, 'line': line_number, 'instances': [],
                           'register_declarations': 0, 'memories': [], 'effects': [], 'ports': [],
                           'intrinsics': {}}
                if name in modules:
                    raise ValueError('Duplicate official module declaration: ' + name)
                modules[name] = current
            elif current is not None:
                match = re.match(r'^\s+inst (\S+) of (\S+)', line)
                if match:
                    current['instances'].append({'name': match[1], 'module': match[2], 'line': line_number})
                if re.match(r'^\s+(?:reg|regreset) ', line):
                    current['register_declarations'] += 1
                if re.match(r'^\s+(?:mem|cmem|smem) ', line):
                    current['memories'].append({'line': line_number, 'declaration': line.strip()})
                if re.match(r'^\s+(?:assert|assume|cover|printf|stop)\s*\(', line):
                    current['effects'].append({'line': line_number, 'statement': line.strip()})
                match = re.match(r'^\s+intrinsic\(([^<,)]+)', line)
                if match:
                    name = match[1]
                    current['intrinsics'][name] = current['intrinsics'].get(name, 0) + 1
                    if name in {'circt_chisel_ifelsefatal', 'circt_chisel_assert', 'circt_chisel_assume',
                                'circt_chisel_cover', 'circt_chisel_printf', 'circt_chisel_stop'}:
                        current['effects'].append({'line': line_number, 'statement': line.strip()})
                match = re.match(r'^    defname = (\S+)', line)
                if match:
                    current['defname'] = match[1]
                match = re.match(r'^    (input|output) ([^:]+): (.*)', line)
                if match:
                    current['ports'].append({'direction': match[1], 'name': match[2].strip(), 'type': match[3]})
    if not modules:
        raise ValueError('No modules found in official FIRRTL output')
    missing = sorted({inst['module'] for m in modules.values() for inst in m['instances']} - modules.keys())
    return {'format': 'xiangshan-declaration-inventory-v2', 'semantics_checked': False,
            'module_definitions': len(modules), 'external_modules': sum(m['kind'] == 'extmodule' for m in modules.values()),
            'instance_declarations': sum(len(m['instances']) for m in modules.values()),
            'register_declarations': sum(m['register_declarations'] for m in modules.values()),
            'memory_declarations': sum(len(m['memories']) for m in modules.values()),
            'effect_statements': sum(len(m['effects']) for m in modules.values()),
            'unresolved_module_names': missing, 'modules': list(modules.values()),
            'scope': 'Declarations in official elaboration. Counts include aggregate registers and are not flattened cell counts. No module is marked Lean-verified by this inventory.'}


def elaborate(config, cores, target, heap, output, rebuild=False):
    if cores < 1 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', config):
        raise ValueError('Expected a configuration class and a positive core count')
    if not re.fullmatch(r'[1-9][0-9]*[mgMG]', heap):
        raise ValueError('Heap must be a JVM size such as 3g')
    attempt = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    folder = output.resolve() / attempt
    folder.mkdir(parents=True)
    cp_file = ROOT / '.cache/xiangshan-classpath.txt'
    if rebuild or not cp_file.is_file():
        command, env = sbt_command(['xiangshanFramework/writeXiangshanClasspath'])
        if run_logged(command, folder / 'build', env=env):
            raise RuntimeError('XiangShan build failed; see ' + str(folder / 'build'))
    lock = json.loads((ROOT / 'verification/xiangshan/config-dependencies.json').read_text())
    # Resource directories are original upstream resources, not replacement RTL.
    resources = sorted((ROOT / lock['cache_directory']).glob('*/src/main/resources'))
    import os
    classpath = cp_file.read_text().strip() + ''.join(os.pathsep + str(p) for p in resources)
    rtl = folder / 'rtl'
    args = ['--config', config, '--num-cores', str(cores), '--target', target,
            '--target-dir', str(rtl), '--full-stacktrace']
    if target == 'systemverilog':
        args += ['--split-verilog', '--firtool-opt', '-O=release --disable-annotation-unknown '
                 '--default-layer-specialization=enable --verification-flavor=immediate '
                 '--mlir-print-op-on-diagnostic=false --disable-all-randomization']
    command = [java_executable(), '-Xmx' + heap, '-XX:ActiveProcessorCount=4',
               '-Dfile.encoding=UTF-8', '-cp', classpath, 'top.TopMain', *args]
    print(f'Official top.TopMain: {config}, {cores} core(s), target={target}; evidence={folder}', flush=True)
    env = rtl_environment()
    env['NOOP_HOME'] = str(folder)
    code = run_logged(command, folder / 'official-generator', cwd=folder, env=env, echo=False)
    files = sorted(p for p in folder.rglob('*') if p.is_file() and
                   (p.suffix in {'.fir', '.sv', '.v', '.f'} or p.name.endswith('.anno.json')))
    report = {'format': 'xiangshan-full-elaboration-v1', 'status': 'PASS' if code == 0 else 'FAIL',
              'generator': 'top.TopMain', 'config': config, 'cores': cores, 'target': target, 'heap': heap,
              'upstream_sources': lock, 'additional_resource_directories': [str(p) for p in resources],
              'classpath_sha256': hashlib.sha256(classpath.encode()).hexdigest(), 'exit_code': code,
              'hardware_source_modified': False, 'lean_conversion': 'NOT_ATTEMPTED',
              'files': {p.relative_to(folder).as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files},
              'evidence': str(folder)}
    for path in files:
        if path.suffix == '.fir':
            inventory = inventory_firrtl(path)
            write(folder / 'module-inventory.json', inventory)
            report['inventory'] = {k: v for k, v in inventory.items() if k != 'modules'}
            break
    write(folder / 'summary.json', report)
    write(output.resolve() / 'latest.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in {'files', 'upstream_sources'}}, ensure_ascii=False, indent=2), flush=True)
    if code:
        stderr = folder / 'official-generator/stderr.log'
        if stderr.is_file():
            print('\n'.join(stderr.read_text(encoding='utf-8').splitlines()[-35:]), file=sys.stderr)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='DefaultConfig')
    parser.add_argument('--cores', type=int, default=1)
    parser.add_argument('--target', choices=['chirrtl', 'systemverilog'], default='chirrtl')
    parser.add_argument('--heap', default='3g')
    parser.add_argument('--out', type=Path, default=ROOT / 'experiments/xiangshan-full-7.3')
    parser.add_argument('--rebuild', action='store_true')
    args = parser.parse_args()
    return elaborate(args.config, args.cores, args.target, args.heap, args.out, args.rebuild)


if __name__ == '__main__':
    raise SystemExit(main())
