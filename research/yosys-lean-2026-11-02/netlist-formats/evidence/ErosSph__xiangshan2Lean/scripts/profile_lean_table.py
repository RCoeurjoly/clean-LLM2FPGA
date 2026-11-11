"""Run a bounded prefix of an existing request with allocator statistics."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from table_simulate import load_interface
from yosys_hierarchy import digest
from yosys_simulate import simulate_yosys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--runner', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or args.evidence.exists():
        parser.error('Choose a positive sample count and a new evidence directory')
    model, request, evidence = args.model.resolve(), args.request.resolve(), args.evidence.resolve()
    with request.open(encoding='utf-8') as stream:
        initial = json.loads(stream.readline())['initial']
        trace = [json.loads(next(stream)) for _ in range(args.samples)]
    manifest = json.loads((model / 'manifest.json').read_text(encoding='utf-8'))
    interface = model / manifest['interface']
    os.environ['MIMALLOC_SHOW_STATS'] = '1'
    code, actual = simulate_yosys(interface, trace, initial, evidence, args.runner.resolve())
    report = {'status': 'PASS' if code == 0 else 'FAIL', 'exit_code': code, 'samples_executed': len(actual),
              'requested_samples': args.samples, 'source_request_sha256': digest(request),
              'runner_manifest_sha256': digest(args.runner / 'manifest.json'),
              'model_manifest_sha256': digest(model / 'manifest.json'),
              'allocator_environment': {'MIMALLOC_SHOW_STATS': '1'},
              'scope': 'Diagnostic prefix only; not a complete whole-chip acceptance run.'}
    (evidence / 'profile.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
