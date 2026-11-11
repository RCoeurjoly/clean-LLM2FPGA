#!/usr/bin/env python3
"""Compare one reset/idle trace of the complete imported model and original RTL."""

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
work = Path('/tmp/llm2fpga-full-hardware-20261102')
rtl = Path('/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl')
icarus = Path('/nix/store/smablxy777s8dd47vcm36qzhl1npjw7m-iverilog-12.0/bin')
lean = '/tmp/llm2fpga-tools/lean-4.19.0-linux/bin/lean'
if not (work / 'TinyStoriesFull.circuit.bin').exists():
    raise SystemExit('Import the full model first.')
shutil.copyfile(root / 'FullReset.lean', work / 'FullReset.lean')
env = os.environ.copy()
env['LEAN_PATH'] = str(work)
report = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
          'scope': 'One whole-design reset/idle trace; no inference or all-input theorem',
          'initial_completion': 'Known initial bits retained; unknown bits and arbitrary inputs zero for this trace only',
          'status': 'running', 'completed': []}
path = root / 'full-reset-result.json'
stages = [
    ('10-native-runtime-c', [lean, '--root=' + str(work), '-c', str(work / 'Functional.c'),
                            str(work / 'Chisel2Lean/Functional.lean')]),
    ('11-native-trace-c', [lean, '--root=' + str(work), '-c', str(work / 'FullReset.c'),
                          str(work / 'FullReset.lean')]),
    ('12-native-link', [str(Path(lean).with_name('leanc')), '-O3', '-o',
                       str(work / 'full-reset-lean'), str(work / 'Functional.c'), str(work / 'FullReset.c')]),
    ('07-rtl-compile', [str(icarus / 'iverilog'), '-g2012', '-s', 'full_reset_tb',
                      '-o', str(work / 'full-reset-sim'), str(root / 'full-reset-tb.sv'),
                      *[str(rtl / name) for name in ['core.sv', 'model.sv', 'memory.sv']]]),
    ('08-rtl-trace', [str(icarus / 'vvp'), str(work / 'full-reset-sim')]),
    ('09-lean-trace', [str(work / 'full-reset-lean'), str(work / 'TinyStoriesFull.circuit.bin')]),
]
for name, command in stages:
    report['active'] = name
    path.write_text(json.dumps(report, indent=2) + '\n')
    result = subprocess.run([sys.executable, str(root / 'run_stage.py'), name, *command], env=env)
    if result.returncode:
        report.update(status='failed', failed_stage=name, returncode=result.returncode)
        break
    report['completed'].append(name)
else:
    try:
        rtl_rows = []
        for line in (root / '08-rtl-trace.log').read_text().splitlines():
            if line.startswith('FULL_RESET '):
                _, index, data, valid, done = line.split()
                rtl_rows.append((int(index), data, valid, done))
        lean_rows = [json.loads(line) for line in (root / '09-lean-trace.log').read_text().splitlines()
                     if line.startswith('{')]
        table_line = next(line for line in (root / '09-lean-trace.log').read_text().splitlines()
                          if line.startswith('C2L_TABLE_CHECK '))
        table_counts = json.loads(table_line.removeprefix('C2L_TABLE_CHECK '))
        imported = json.loads((root / 'import-summary.json').read_text())['imported']
        assert table_counts == {key: imported[key] for key in table_counts}
        assert len(rtl_rows) == len(lean_rows) == 8, (len(rtl_rows), len(lean_rows))
        comparisons = 0
        for index, data, valid, done in rtl_rows:
            if index < 3:
                continue  # Skip initial X-valued FF observations, as in the memory control.
            expected = {'logits_data': int(data, 16), 'logits_valid': int(valid, 2), 'done': int(done, 2)}
            assert lean_rows[index]['sample'] == index
            assert lean_rows[index]['outputs'] == expected, (index, expected, lean_rows[index])
            comparisons += len(expected)
        report.update(status='completed', trace_samples=8, compared_samples_after_reset=5,
                      excluded_initial_samples=[0, 1, 2], output_comparisons=comparisons,
                      table_counts=table_counts, runtime='Unchanged upstream Lean runtime, natively compiled; binary input')
    except (AssertionError, KeyError, ValueError) as error:
        report.update(status='failed', failed_stage='trace-comparison', error=repr(error))
report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
path.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
raise SystemExit(0 if report['status'] == 'completed' else 1)
