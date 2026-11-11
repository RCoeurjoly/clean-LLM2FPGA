#!/usr/bin/env python3
"""Compare completed whole-design reset/idle simulation logs."""

import argparse
import json
from pathlib import Path

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--rtl-log', type=Path, required=True)
parser.add_argument('--lean-log', type=Path, required=True)
parser.add_argument('--simulator', required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
report = {'scope': 'One whole-design reset/idle trace; no inference or all-input theorem',
          'simulator': args.simulator, 'rtl_log': str(args.rtl_log), 'lean_log': str(args.lean_log),
          'initial_completion': 'Lean known bits retained; unknown bits/arbitrary inputs zero for this trace only'}
try:
    rtl_rows = []
    for line in args.rtl_log.read_text().splitlines():
        if line.startswith('FULL_RESET '):
            _, index, data, valid, done = line.split()
            rtl_rows.append((int(index), data, valid, done))
    lean_text = args.lean_log.read_text()
    lean_rows = [json.loads(line) for line in lean_text.splitlines() if line.startswith('{')]
    checks = [json.loads(line.removeprefix('C2L_TABLE_CHECK ')) for line in lean_text.splitlines()
              if line.startswith('C2L_TABLE_CHECK ')]
    imported = json.loads((root / 'import-summary.json').read_text())['imported']
    expected_counts = {key: imported[key] for key in ['nodes', 'inputs', 'states', 'outputs']}
    assert checks == [expected_counts], checks
    assert [row[0] for row in rtl_rows] == list(range(8)), rtl_rows
    assert [row['sample'] for row in lean_rows] == list(range(8)), lean_rows
    for index, data, valid, done in rtl_rows[3:]:
        expected = {'logits_data': int(data, 16), 'logits_valid': int(valid, 2), 'done': int(done, 2)}
        assert lean_rows[index]['outputs'] == expected, (index, expected, lean_rows[index])
    report.update(status='completed', trace_samples=8, compared_samples_after_reset=5,
                  excluded_initial_samples=[0, 1, 2], output_comparisons=15, table_counts=expected_counts)
except (AssertionError, KeyError, ValueError) as error:
    report.update(status='failed', error=repr(error))
args.report.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
raise SystemExit(0 if report['status'] == 'completed' else 1)
