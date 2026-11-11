"""Build the official pinned Spike reference locally, with commit logging."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import shutil
import subprocess

from common import ROOT, run_logged
from server import stamp
from server_bundle import write
from xiangshan_isa import SPIKE_COMMIT
from yosys_hierarchy import digest

URL = 'https://github.com/riscv-software-src/riscv-isa-sim.git'
SOURCE = ROOT / '.tools/spike-v1.1.0'


def install(jobs=16):
    evidence = ROOT / '.tools/spike-bootstrap' / stamp()
    evidence.mkdir(parents=True)
    if not SOURCE.exists():
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        if run_logged(['git', '-c', 'http.version=HTTP/1.1', 'clone', '--depth', '1', '--branch', 'v1.1.0', URL, str(SOURCE)],
                      evidence / 'clone', echo=False):
            raise RuntimeError('Official Spike checkout failed: ' + str(evidence))
    commit = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != SPIKE_COMMIT or subprocess.run(['git', '-C', str(SOURCE), 'diff', '--quiet', 'HEAD', '--']).returncode:
        raise ValueError('Preserve the existing source; expected an unchanged official Spike ' + SPIKE_COMMIT)
    build = SOURCE / 'build'
    build.mkdir(exist_ok=True)
    destination = (SOURCE / 'install').resolve()
    if not (build / 'config.h').exists():
        if run_logged([str(SOURCE.resolve() / 'configure'), '--prefix='+str(destination), '--enable-commitlog'],
                      evidence / 'configure', cwd=build, echo=False):
            raise RuntimeError('Spike configure failed; see ' + str(evidence))
    if not re.search(r'^\s*#define\s+RISCV_ENABLE_COMMITLOG\b', (build / 'config.h').read_text(), re.MULTILINE):
        raise ValueError('Existing Spike build lacks commit logging; keep it and configure a separate build')
    for name, command in [('build', ['make', '-j'+str(jobs)]), ('install', ['make', 'install'])]:
        if run_logged(command, evidence / name, cwd=build, echo=False):
            raise RuntimeError('Spike '+name+' failed; see '+str(evidence))
    dependencies = [build / ('lib'+name+'.a') for name in ['riscv','disasm','softfloat','fesvr','fdt']]
    dependencies += [build / 'config.h', SOURCE / 'LICENSE', destination / 'bin/spike']
    compiler = Path(shutil.which('g++') or '')
    if not compiler.is_file():
        raise RuntimeError('A C++ compiler is required')
    report = {'status':'BUILT', 'repository':URL, 'commit':commit, 'tag':'v1.1.0', 'commit_logging':True,
              'compiler_sha256':digest(compiler), 'compiler_version':subprocess.check_output([str(compiler),'--version'],text=True).splitlines()[0],
              'files_sha256':{str(path.resolve().relative_to(SOURCE.resolve())):digest(path) for path in dependencies},
              'evidence':str(evidence)}
    write(SOURCE / 'chisel2lean-lock.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=16)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('Jobs must be positive')
    install(args.jobs)
