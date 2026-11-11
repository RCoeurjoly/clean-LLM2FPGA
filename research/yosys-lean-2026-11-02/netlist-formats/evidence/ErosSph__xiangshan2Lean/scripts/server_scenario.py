"""Replay another physical-input scenario on the saved complete XSTop and Lean model.

The checked bundle's initial state and zero arbitrary-input choices are retained.
Only the 68 physical inputs change; original RTL, circuit data, and prior results
are preserved. Run on the server, where the complete model and build cache live.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from server import (build_native, compile_model, doctor, load_work, native_run,
                    rtl_whole, stamp)
from server_bundle import MANIFEST, read, verify_bundle, write
from trace_compare import compare_rows
from yosys_hierarchy import digest


def validate_trace(trace, metadata):
    ports = {p['name']: p for p in metadata['ports'] if p['direction'] == 'input'}
    if not isinstance(trace, list) or not trace:
        raise ValueError('Supply a nonempty list of physical-input samples')
    for index, row in enumerate(trace):
        if not isinstance(row, dict) or set(row) != {'inputs'}:
            raise ValueError('Expected only physical inputs at sample ' + str(index))
        values = row['inputs']
        if not isinstance(values, dict) or set(values) != set(ports):
            raise ValueError('Missing or extra physical inputs at sample ' + str(index))
        for name, value in values.items():
            if type(value) not in (int, bool) or not 0 <= value < (1 << ports[name]['width']):
                raise ValueError('Out-of-width physical input at sample ' + str(index) + ': ' + name)


def write_request(template, destination, trace, metadata, counts):
    """Keep the recorded state/header and cuts; replace only physical inputs."""
    validate_trace(trace, metadata)
    physical = {p['name'] for p in metadata['ports'] if p['direction'] == 'input'}
    with template.open('rb') as stream:
        header = stream.readline()
        initial = json.loads(header)
        inputs = json.loads(stream.readline())['inputs']
    if set(initial) != {'initial'} or len(initial['initial']) != counts['states']:
        raise ValueError('Initial state template does not match the saved complete model')
    if len(inputs) != counts['inputs'] or not physical <= inputs.keys():
        raise ValueError('Input template does not match the saved complete model')
    if any(value != 0 for name, value in inputs.items() if name not in physical):
        raise ValueError('Expected the recorded zero arbitrary-input experiment')
    with destination.open('xb') as stream:
        stream.write(header if header.endswith(b'\n') else header + b'\n')
        for row in trace:
            inputs.update(row['inputs'])
            stream.write((json.dumps({'inputs': inputs}, separators=(',', ':')) + '\n').encode('utf-8'))
    return {'initial_header_sha256': hashlib.sha256(header).hexdigest(),
            'template_request_sha256': digest(template), 'request_sha256': digest(destination),
            'arbitrary_inputs': counts['inputs'] - len(physical), 'arbitrary_choice': 0}


def reset_scenario(original):
    """One global-sample reset pulse between main edges, independent JTAG."""
    trace = []
    for i, row in enumerate(original):
        values = dict(row['inputs'])
        values.update(io_reset=int(i < 32 or i == 72),
                      io_systemjtag_reset=int(i < 16 or 65 <= i < 67),
                      io_systemjtag_jtag_TCK=(i // 3) % 2)
        trace.append({'inputs': values})
    if len(trace) < 80:
        raise ValueError('Reset scenario needs at least 80 global samples')
    return trace


def replay(root, work, scenario, jobs=4):
    bundle, _ = verify_bundle(root)
    load_work(root, work)
    host = doctor(True)
    metadata = read(root / 'scenario/ports.json')
    trace = scenario['trace']
    validate_trace(trace, metadata)
    execution = work / 'scenarios' / stamp()
    execution.mkdir(parents=True)
    write(execution / 'scenario.json', scenario)
    summary = {'status': 'PREPARED', 'description': scenario['description'],
               'bundle_manifest_sha256': digest(root / MANIFEST),
               'scenario_sha256': digest(execution / 'scenario.json'),
               'samples': len(trace), 'execution': str(execution),
               'instruction_execution': 'NOT_TESTED', 'universal_equivalence': 'NOT_PROVED'}
    write(execution / 'summary.json', summary)
    print('SCENARIO_DIRECTORY=' + str(execution), flush=True)
    try:
        summary.update(write_request(root / 'scenario/request.jsonl', execution / 'request.jsonl',
                                     trace, metadata, bundle['model']['counts']))
        summary['status'] = 'RUNNING_RTL'
        write(execution / 'summary.json', summary)
        reference = rtl_whole(root, work, bundle, jobs, trace=trace, execution=execution / 'rtl',
                              record=execution / 'rtl.json')
        print('PASS complete original RTL scenario', flush=True)
        compile_model(work, 'model', host)
        build_native(root, work)
        summary['status'] = 'RUNNING_LEAN'
        write(execution / 'summary.json', summary)
        actual = native_run(root, work, 'model', execution / 'request.jsonl', execution / 'lean')
        ports = [p['name'] for p in metadata['ports'] if p['direction'] == 'output']
        comparison = compare_rows(actual, reference, ports, len(trace))
        verify_bundle(root)
        report = {**summary, **comparison,
                  'top': bundle['top'], 'config': bundle['config'], 'cores': bundle['cores'],
                  'upstream_commit': bundle['upstream_commit'], 'original_hardware_modified': False,
                  'lean_result_sha256': digest(execution / 'lean/result.json'),
                  'rtl_result_sha256': digest(execution / 'rtl/result.json'),
                  'rtl_startup': read(execution / 'rtl/startup.json'),
                  'rtl_driver': read(execution / 'rtl/driver.json')}
        write(execution / 'acceptance.json', report)
        if report['status'] != 'PASS':
            raise AssertionError('Whole scenario comparison failed: ' + report['status'])
        summary.update(report)
    except Exception as error:
        summary.update(status='FAILED', error=str(error))
        raise
    finally:
        write(execution / 'summary.json', summary)
    print(json.dumps({key: report[key] for key in ('status', 'samples', 'output_ports', 'output_comparisons', 'execution')}, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--scenario', type=Path, help='JSON with description and trace (complete physical inputs)')
    choice.add_argument('--reset-reassertion', action='store_true')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('Jobs must be positive')
    root, work = args.bundle.resolve(), args.work.resolve()
    scenario = read(args.scenario) if args.scenario else {
        'description': 'Asynchronous one-sample main reset reassertion between rising edges; independent JTAG clock and reset pulse; original stalled AXI/query inputs.',
        'trace': reset_scenario(read(root / 'scenario/trace.json'))}
    replay(root, work, scenario, args.jobs)


if __name__ == '__main__':
    main()
