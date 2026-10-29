#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess

root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
names = set(subprocess.check_output([
    'git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'
]).decode().split('\0'))
counts = {}
for name in sorted(names):
    path = root / name
    if path.is_file() and not path.is_symlink():
        data = path.read_bytes()
        if b'\0' not in data:
            counts[name] = len(data.splitlines())
print(json.dumps({'total_lines': sum(counts.values()), 'files': len(counts)}, indent=2))
