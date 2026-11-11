#!/usr/bin/env python3
"""Record a research command, its output and measured resource consumption."""

import datetime as dt
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

directory = Path(__file__).parent
name, *command = sys.argv[1:]
if not command or '/' in name or name.startswith('.'):
    raise SystemExit('usage: run_stage.py STAGE COMMAND ARGUMENTS...')
report = {'stage': name, 'command': command, 'cwd': str(Path.cwd()),
          'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
          'inherited_limits': {'address_space': resource.getrlimit(resource.RLIMIT_AS),
                               'cpu_seconds': resource.getrlimit(resource.RLIMIT_CPU)},
          'status': 'running'}
path = directory / (name + '.json')
path.write_text(json.dumps(report, indent=2) + '\n')
started = time.monotonic()
with (directory / (name + '.log')).open('w') as output:
    result = subprocess.run(['/usr/bin/time', '-v', '-o', str(directory / (name + '.resources.txt')),
                             *command], stdout=output, stderr=subprocess.STDOUT)
report.update(returncode=result.returncode, status='completed' if result.returncode == 0 else 'failed',
              elapsed_seconds=time.monotonic() - started,
              finished_at=dt.datetime.now(dt.timezone.utc).isoformat())
path.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'stage': name, 'status': report['status'],
                  'elapsed_seconds': report['elapsed_seconds'], 'returncode': result.returncode}))
raise SystemExit(result.returncode)
