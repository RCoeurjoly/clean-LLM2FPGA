#!/usr/bin/env python3
"""Run the approved hardware import spike from a production checkpoint."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

if len(sys.argv) != 1:
    raise SystemExit('This one-shot probe accepts no arguments; stages are always rerun.')

root = Path(__file__).resolve().parent
repo = root.parents[2]
work = Path('/tmp/llm2fpga-full-hardware-20261102')
work.mkdir(parents=True, exist_ok=True)
upstream = root.parent / 'netlist-formats/evidence/ErosSph__xiangshan2Lean'
yosys = '/nix/store/vjqba2jj3231f14vhspj2mvs1k1z4mm4-llm2fpga-yosys-sdp72-parity/bin/yosys'
lean = '/tmp/llm2fpga-tools/lean-4.19.0-linux/bin/lean'
checkpoint = Path('/nix/store/nmncbcxfrj9qxb3cpx726ay4qvgx6122-tinystories-prepared/model.il')
if not checkpoint.exists():
    raise SystemExit('Build direct-prepared first; the expected checkpoint is absent.')
env = os.environ.copy()
env['LEAN_PATH'] = str(work)
env['PYTHONDONTWRITEBYTECODE'] = '1'
worker = [sys.executable, str(root / 'import_worker.py')]
common = ['--upstream', str(upstream), '--work', str(work)]
stages = [
    ('01-generic-json', [yosys, '-Q', '-T', '-s', str(root / '01-generic-json.ys')]),
    ('01-preflight', worker + ['audit', *common, '--report', str(root / 'preflight.json')]),
    ('02-normalize', [yosys, '-Q', '-T', '-s', str(root / '02-normalize.ys')]),
    ('03-functional-export', [yosys, '-Q', '-T', '-s', str(root / '03-functional-export.ys')]),
    ('04-import', worker + ['import', *common, '--report', str(root / 'import.json')]),
    ('05-runtime-compile', [lean, '--root=' + str(work), '-o',
        str(work / 'Chisel2Lean/Functional.olean'), str(work / 'Chisel2Lean/Functional.lean')]),
    ('06-model-compile', [lean, '--root=' + str(work), '-o',
        str(work / 'TinyStoriesFull.olean'), str(work / 'TinyStoriesFull.lean')]),
]
status = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
          'status': 'running', 'completed': [], 'checkpoint': str(checkpoint),
          'equivalence_proved': False}
path = root / 'pipeline-status.json'
for name, command in stages:
    status['active'] = name
    path.write_text(json.dumps(status, indent=2) + '\n')
    result = subprocess.run([sys.executable, str(root / 'run_stage.py'), name, *command],
                            cwd=repo, env=env)
    if result.returncode:
        status.update(status='failed', failed_stage=name, returncode=result.returncode)
        break
    status['completed'].append(name)
else:
    status['status'] = 'completed'
status['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
path.write_text(json.dumps(status, indent=2) + '\n')
raise SystemExit(0 if status['status'] == 'completed' else 1)
