"""Aggregate an already launched finite batch; never rerun or mark unfinished jobs as passed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from program_report import collect
from server_bundle import read, write
from yosys_hierarchy import digest


def snapshot(job):
    log = Path(job['stdout'])
    lines = log.read_text(encoding='utf-8', errors='replace').splitlines() if log.exists() else []
    locations = [line.split('=',1)[1] for line in lines if line.startswith('ACCEPTANCE_PROGRAM_DIRECTORY=')]
    command = job['command']
    if locations:
        case = Path(locations[-1])
    elif '--existing-program' in command:
        case = Path(command[command.index('--existing-program')+1])
    else:
        case = None
    result = {'name':job['name'], 'pid':job['pid'], 'case':str(case) if case else None, 'status':'RUNNING'}
    if case and (case / 'program-acceptance.json').exists():
        path = case / 'program-acceptance.json'
        receipt = read(path)
        result.update(status=receipt['status'], receipt=str(path), receipt_sha256=digest(path))
        if receipt['status'] in {'RTL_LEAN_SPIKE_PROGRAM_PASS','FAILED'}:
            return result
    # These are jobs on this Linux server. A recycled PID cannot keep a batch alive.
    path = Path('/proc') / str(job['pid']) / 'cmdline'
    running = path.exists() and b'validate_xiangshan_program.py' in path.read_bytes()
    if not running:
        result.update(status='PROCESS_EXITED_WITHOUT_FINAL_RECEIPT')
    return result


def finalize(work, jobs, report, output, interval=30):
    configurations = read(jobs)
    while True:
        try:
            states = [snapshot(job) for job in configurations]
            result = collect(work, report)
            done = all(job['status'] != 'RUNNING' for job in states)
            passed = all(job['status'] == 'RTL_LEAN_SPIKE_PROGRAM_PASS' for job in states)
            state = {'status':'PASS' if passed else 'COMPLETED_WITH_FAILURES' if done else 'RUNNING',
                     'updated_at_utc':datetime.now(timezone.utc).isoformat(), 'jobs':states,
                     'jobs_sha256':digest(jobs), 'program_report':str(report),
                     'finalizer_source_sha256':digest(Path(__file__)),
                     'accepted_three_way_program_cases':result['accepted_three_way_program_cases'],
                     'universal_equivalence':'NOT_PROVED'}
            write(output,state)
            if done:
                print(json.dumps(state,indent=2),flush=True)
                return state
        except (json.JSONDecodeError, FileNotFoundError):
            # Another job may be in the middle of writing its small receipt.
            pass
        time.sleep(interval)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['work','jobs','report','out']:
        parser.add_argument('--'+name,type=Path,required=True)
    args = parser.parse_args()
    finalize(*(getattr(args,name).resolve() for name in ['work','jobs','report','out']))
