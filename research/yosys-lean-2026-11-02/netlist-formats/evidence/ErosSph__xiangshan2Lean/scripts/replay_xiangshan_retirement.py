"""Compare original RTL ports, read-only taps and retirement with actual Lean execution."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import time

from compact_request import csv_inputs
from model_request import write_model_request
from server_bundle import read, write, verify_bundle
from yosys_hierarchy import digest

GPR_MEMORY = 'core_with_l2.core.exuBlocks.scheduler.intRfReadData_regfile.mem_ext.Memory'


def retired_pc(values, entry, offset):
    if not 0 <= entry < 64 or not 0 <= offset < 16:
        raise ValueError('Invalid Lean FTQ index/offset')
    base = 'c2l_observe_pc_'+str(entry)+'_'
    start, next_line = values[base+'startAddr'], values[base+'nextLineAddr']
    high = next_line >> 6 if values[base+'isNextMask_'+str(offset)] and (start >> 5) & 1 else start >> 6
    pc = (high << 6) | (((((start >> 1) & 31) + offset) & 31) << 1)
    return pc | (((1 << 64)-1) ^ ((1 << 39)-1)) if pc & (1 << 38) else pc


def replay(root, model_dir, program, runner, validation, audit_every=256, evidence=None):
    verify_bundle(root)
    engine = read(runner / 'manifest.json')
    if read(validation)['status'] != 'PASS' or read(validation)['runner_manifest_sha256'] != digest(runner / 'manifest.json'):
        raise ValueError('Missing matching successful runner regression')
    for name, key in [('Functional.lean', 'runtime_sha256'), ('Incremental.lean', 'incremental_source_sha256'),
                      ('Progressive.lean', 'progressive_source_sha256')]:
        if digest(runner / 'Chisel2Lean' / name) != engine.get(key):
            raise ValueError('Expected the checked progressive runner with state observations')
    if 'compiled_source_sha256' in engine and digest(runner / 'Chisel2Lean/Compiled.lean') != engine['compiled_source_sha256']:
        raise ValueError('Verified inline helpers changed')
    executable = Path(engine['executable'])
    if digest(executable) != engine['executable_sha256']:
        raise ValueError('Runner executable changed')
    manifest = read(model_dir / 'manifest.json')
    if not manifest.get('lean_checked'):
        raise ValueError('Observed Lean model must have completed its Lean compilation checks')
    original = read(root / 'model/origin-manifest.json')
    if manifest['source_files_sha256'] != original['source_files_sha256']:
        raise ValueError('Observed Lean model does not come from the same complete original RTL')
    if manifest['table_runtime_sha256'] != engine['runtime_sha256']:
        raise ValueError('Lean model and executable use different semantics')
    binary = model_dir / manifest['table']['binary']
    if digest(binary) != manifest['artifact_sha256'][manifest['table']['binary']]:
        raise ValueError('Observed circuit packet changed')
    taps = manifest['read_only_observations']
    if taps != read(program / 'observations.json'):
        raise ValueError('RTL and Lean do not observe exactly the same original wires')
    isa = read(program / 'isa-comparison/summary.json')
    if isa['status'] != 'RTL_SPIKE_PC_GPR_PASS' or isa['rtl_commits_sha256'] != digest(program / 'commits.csv'):
        raise ValueError('Retirement acceptance requires the same completed RTL/Spike PC/GPR test')
    if isa.get('program_elf_sha256', digest(program / 'program.elf')) != digest(program / 'program.elf') or \
       isa['program_sha256'] != digest(program / 'program.bin') or \
       ((program / 'segments.csv').exists() and 'program_elf_sha256' not in isa):
        raise ValueError('ISA receipt belongs to another program ELF')
    rtl = read(program / 'execution.json')
    if rtl['status'] != 'RTL_PROGRAM_COMPLETED' or rtl['signature'] != 1:
        raise ValueError('Original RTL program did not finish')
    directory = evidence.resolve() if evidence else program / 'lean-retirement-replay'
    directory.mkdir(exist_ok=False)
    physical = read(root / 'scenario/ports.json')['ports']
    inputs = [p['name'] for p in physical if p['direction'] == 'input']
    outputs = [p['name'] for p in physical if p['direction'] == 'output']
    output_names = set(outputs) | {tap['name'] for tap in taps}
    request = directory / 'request.jsonl'
    prepared = write_model_request(model_dir, request, inputs, csv_inputs(program / 'inputs.csv'),
                                   audit_every, [GPR_MEMORY])
    if prepared['samples'] != rtl['samples']:
        raise ValueError('Incomplete physical input trace')
    report = {'status': 'RUNNING', 'request': prepared, 'samples_expected': rtl['samples'],
              'physical_outputs': len(outputs), 'read_only_taps': len(taps), 'commits_expected': rtl['committed'],
              'model_manifest_sha256': digest(model_dir / 'manifest.json'),
              'circuit_sha256': digest(binary), 'runner_manifest_sha256': digest(runner / 'manifest.json'),
              'runner_validation_sha256': digest(validation), 'isa_reference_sha256': digest(program / 'isa-comparison/summary.json'),
              'program_sha256': digest(program / 'program.bin'),
              'program_elf_sha256': digest(program / 'program.elf'),
              'rtl_trace_sha256': {name: digest(program / name) for name in ['inputs.csv', 'outputs.csv', 'observations.csv', 'commits.csv']},
              'original_hardware_modified': False, 'universal_equivalence': 'NOT_PROVED'}
    write(directory / 'summary.json', report)
    command = [str(executable), str(binary), str(request)]
    write(directory / 'command.json', {'command': command, 'cwd': str(runner)})
    began = time.monotonic()
    count = committed = 0
    pending = None
    output_hash = hashlib.sha256()
    with (directory / 'stderr.log').open('w') as errors, \
         (directory / 'stdout.jsonl').open('w') as saved, \
         (directory / 'lean-commits.jsonl').open('w') as commit_saved, \
         (program / 'outputs.csv').open(newline='') as port_stream, \
         (program / 'observations.csv').open(newline='') as tap_stream, \
         (program / 'commits.csv').open(newline='') as commit_stream:
        ports, observations, commits = (iter(csv.DictReader(stream)) for stream in [port_stream, tap_stream, commit_stream])
        process = subprocess.Popen(command, cwd=runner, stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8')
        write(directory / 'process.json', {'pid': process.pid, 'command': command})
        try:
            for line in process.stdout:
                saved.write(line)
                output_hash.update(line.encode('utf-8'))
                actual = json.loads(line)
                reference, observed = next(ports, None), next(observations, None)
                if reference is None or observed is None or actual.get('cycle') != count or \
                   int(reference.pop('sample')) != count or int(observed.pop('sample')) != count:
                    raise ValueError('Missing/reordered sample at ' + str(count))
                expected = {key: int(value, 0) for key, value in {**reference, **observed}.items()}
                for phase in ['pre', 'post']:
                    values = actual[phase]
                    if set(values) != output_names or set(expected) != output_names:
                        raise ValueError('Physical/observation output set differs')
                    differences = {key: {'lean': values[key], 'rtl': expected[key]} for key in output_names if values[key] != expected[key]}
                    if differences:
                        write(directory / 'counterexample.json', {'sample': count, 'phase': phase, 'differences': differences})
                        raise ValueError('RTL/Lean wire mismatch at sample ' + str(count))
                values = actual['post']
                cycle, phase = divmod(count, 4)
                if phase == 1:
                    entries = []
                    memory = actual['state_before'][GPR_MEMORY]
                    for lane in range(6):
                        if values['c2l_observe_is_commit'] and values['c2l_observe_valid_'+str(lane)]:
                            destination = values['c2l_observe_ldest_'+str(lane)]
                            physical_destination = values['c2l_observe_pdest_'+str(lane)]
                            enabled = values['c2l_observe_rfWen_'+str(lane)]
                            if enabled and destination and not 0 <= physical_destination < 192:
                                raise ValueError('Invalid Lean physical GPR destination')
                            entries.append({'cycle': cycle, 'lane': lane, 'ldest': destination, 'pdest': physical_destination,
                                            'rfwen': enabled, 'data': memory[physical_destination] if enabled and destination else 0})
                    pending = (entries, values, bool(values['c2l_observe_flush']))
                if phase == 2:
                    if pending is None:
                        raise ValueError('Missing pre-edge retirement snapshot')
                    entries, before, flush = pending
                    for entry in entries:
                        lane = str(entry['lane'])
                        if flush and lane != '0':
                            raise ValueError('Unexpected multi-lane retirement at ROB flush')
                        index = values['c2l_observe_flush_ftqIdx_value' if flush else 'c2l_observe_ftqIdx_value_'+lane]
                        offset = values['c2l_observe_flush_ftqOffset' if flush else 'c2l_observe_ftqOffset_'+lane]
                        entry['pc'] = retired_pc(before, index, offset)
                        expected_commit = next(commits, None)
                        if expected_commit is None or any(entry[key] != int(expected_commit[key], 0) for key in entry):
                            write(directory / 'commit-counterexample.json', {'commit': committed, 'lean': entry, 'rtl': expected_commit})
                            raise ValueError('RTL/Lean retirement differs at commit ' + str(committed))
                        commit_saved.write(json.dumps(entry, separators=(',', ':'))+'\n')
                        committed += 1
                    pending = None
                count += 1
                if count % 256 == 0:
                    saved.flush()
                    write(directory / 'progress.json', {'samples': count, 'expected': rtl['samples'], 'committed': committed,
                          'elapsed_seconds': time.monotonic()-began})
                    print(f'RETIREMENT REPLAY {count}/{rtl["samples"]} commits={committed}', flush=True)
            code = process.wait()
            if code or count != rtl['samples'] or committed != rtl['committed'] or \
               any(next(reader, None) is not None for reader in [ports, observations, commits]):
                raise RuntimeError(f'Incomplete replay: exit={code}, samples={count}, commits={committed}')
            for name, expected_hash in report['rtl_trace_sha256'].items():
                if digest(program / name) != expected_hash:
                    raise ValueError('Recorded RTL trace changed during replay: '+name)
            if digest(request) != prepared['request_sha256'] or digest(binary) != report['circuit_sha256'] or \
               digest(model_dir / 'manifest.json') != report['model_manifest_sha256'] or \
               digest(executable) != engine['executable_sha256']:
                raise ValueError('Lean request, model or executable changed during replay')
            report.update(status='RTL_LEAN_RETIREMENT_PASS', samples=count, committed=committed,
                          wire_comparisons=count*2*len(output_names), stdout_sha256=output_hash.hexdigest(),
                          elapsed_seconds=time.monotonic()-began,
                          scope='Finite program only. Every physical output and selected original internal wire matches at every sample; actual Lean retirement PC/GPR data matches original RTL, whose retirement also matches official Spike.')
        except Exception as error:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            report.update(status='FAILED', samples=count, committed=committed, error=str(error))
            raise
        finally:
            process.stdout.close()
            write(directory / 'summary.json', report)
    print(json.dumps({key: report[key] for key in ['status', 'samples', 'committed', 'wire_comparisons', 'elapsed_seconds']}, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['bundle', 'model', 'program', 'runner', 'validation']:
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--audit-every', type=int, default=256)
    parser.add_argument('--out', type=Path, help='Fresh evidence directory; existing results are preserved')
    args = parser.parse_args()
    if args.audit_every < 1:
        parser.error('Enable periodic complete-evaluation audits')
    replay(args.bundle.resolve(), args.model.resolve(), args.program.resolve(), args.runner.resolve(),
           args.validation.resolve(), args.audit_every, args.out)
