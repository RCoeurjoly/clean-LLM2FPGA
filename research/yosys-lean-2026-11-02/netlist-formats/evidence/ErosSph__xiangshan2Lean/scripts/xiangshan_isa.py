"""Compare original RTL retirement with the pinned official Spike reference."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import subprocess

from common import ROOT, run_logged
from server_bundle import read, write
from yosys_hierarchy import digest

SPIKE_COMMIT = '530af85d83781a3dae31a4ace84a573ec255fefa'
COMMIT = re.compile(r'^core\s+0:\s+([0-3])\s+(0x[0-9a-f]+)\s+\((0x[0-9a-f]+)\)(.*)$')
GPR = re.compile(r'\bx\s*(\d+)\s+(0x[0-9a-f]+)')


def parse_commits(path):
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        match = COMMIT.fullmatch(line)
        if match is None:
            raise ValueError('Unrecognized official Spike commit record: ' + line)
        privilege, pc, instruction, details = match.groups()
        writes = [(int(index), int(value, 16)) for index, value in GPR.findall(details) if int(index)]
        if len(writes) > 1:
            raise ValueError('Expected at most one integer GPR destination per instruction')
        result.append({'pc': int(pc, 16), 'instruction': int(instruction, 16),
                       'privilege': int(privilege), 'gpr_writes': writes})
    return result


def compare(program, reference, target):
    with (program / 'commits.csv').open(newline='', encoding='utf-8') as stream:
        actual = list(csv.DictReader(stream))
    expected = parse_commits(reference / 'spike-commits.log')
    with (reference / 'registers.csv').open(newline='', encoding='utf-8') as stream:
        registers = list(csv.DictReader(stream))
    if not expected or len(expected) != len(registers) or len(actual) != len(expected):
        raise ValueError(f'Retirement counts differ: RTL={len(actual)}, Spike={len(expected)}, snapshots={len(registers)}')
    pc_observed = 'pc' in actual[0]
    positions = [(int(row['cycle']), int(row['lane'])) for row in actual]
    if positions != sorted(set(positions)) or any(cycle < 0 or not 0 <= lane < 6 for cycle, lane in positions):
        raise ValueError('RTL retirement rows are missing their ordered cycle/lane positions')
    image = [0] * 32
    writes = 0
    for index, (rtl, isa, snapshot) in enumerate(zip(actual, expected, registers)):
        if int(snapshot['commit']) != index:
            raise ValueError('Spike register snapshots are missing/reordered')
        destination = int(rtl['ldest'])
        if not 0 <= destination < 32:
            raise ValueError('Invalid RTL architectural GPR destination')
        observed = [(destination, int(rtl['data']))] if int(rtl['rfwen']) and destination else []
        mismatch = None
        if observed != [tuple(entry) for entry in isa['gpr_writes']]:
            mismatch = 'integer register writeback'
        elif pc_observed and int(rtl['pc'], 0) != isa['pc']:
            mismatch = 'retired PC'
        if mismatch:
            write(target / 'counterexample.json', {'kind': mismatch, 'commit': index, 'rtl': rtl, 'spike': isa})
            raise ValueError(f'{mismatch} differs at commit {index}')
        for register, value in observed:
            image[register] = value
            writes += 1
        if image != [int(snapshot['x'+str(register)]) for register in range(32)]:
            write(target / 'counterexample.json', {'kind': 'reconstructed architectural GPRs', 'commit': index,
                  'rtl': image, 'spike': snapshot})
            raise ValueError(f'Architectural GPR reconstruction differs at commit {index}')
    return {'status': 'RTL_SPIKE_PC_GPR_PASS' if pc_observed else 'RTL_SPIKE_GPR_PASS',
            'committed_instructions': len(expected), 'integer_writebacks': writes,
            'retired_pc_comparison': 'PASS' if pc_observed else 'NOT_OBSERVED',
            'integer_writeback_comparison': 'PASS', 'reconstructed_gprs_after_every_commit': 'PASS',
            'actual_retired_instruction_bits': 'NOT_OBSERVED', 'full_fp_csr_state_comparison': 'NOT_RUN',
            'lean_retirement_comparison': 'NOT_RUN', 'universal_equivalence': 'NOT_PROVED'}


def check_writeback(program, reference, target):
    """For the cache test's write-once data, check actual DRAM writeback bytes."""
    execution = read(program / 'execution.json')
    if execution.get('dram_writes', 0) < 1:
        raise ValueError('The cache test did not cause an external DRAM writeback')
    def memory(path):
        with path.open(newline='', encoding='utf-8') as stream:
            return {int(row['address']): int(row['data']) for row in csv.DictReader(stream)}
    actual = {address: value for address, value in memory(program / 'external-memory.csv').items()
              if 0x80000000 <= address < 0x88000000}
    expected = memory(reference / 'reference-memory.csv')
    if not actual:
        raise ValueError('No external DRAM bytes were recorded')
    for address, value in actual.items():
        if value != expected.get(address, 0):
            write(target / 'writeback-counterexample.json', {'address': address, 'rtl': value,
                  'spike': expected.get(address, 0)})
            raise ValueError('External memory writeback differs from Spike at ' + hex(address))
    return {'status': 'PASS', 'external_dram_write_requests': execution['dram_writes'],
            'bytes_compared': len(actual),
            'scope': 'Final external DRAM bytes versus Spike for this write-once cache test; cache-resident bytes are not claimed to be written back.'}


def interrupt_boundary(program, target, total):
    """Synchronize a cycle-free ISA model at a measured precise trap boundary."""
    path = program / 'environment.json'
    if not path.exists() or not read(path).get('irq_on_read_wait'):
        return None
    environment = read(path)
    provenance = read(program / 'provenance.json')
    if provenance.get('environment_sha256') != digest(path):
        raise ValueError('External test environment changed')
    if (environment['external_irq_bits'], environment['delayed_read_address'],
        environment['extra_read_latency_cycles'], environment['test_device_initial_value']) != (1, 0x50000000, 500, 0x12345678):
        raise ValueError('Unsupported interrupt/read-wait environment')
    with (program / 'events.csv').open(newline='', encoding='utf-8') as stream:
        events = list(csv.DictReader(stream))
    if len(events) != 2 or [r['event'] for r in events] != ['assert_during_read_wait', 'interrupt_accepted']:
        raise ValueError('Expected one external interrupt asserted during a read wait and then accepted')
    assertion, accepted = events
    cycle, trap_cycle = int(assertion['cycle']), int(accepted['cycle'])
    ordinal, mepc = int(accepted['committed']), int(accepted['detail'])
    if not (0 <= cycle < trap_cycle and cycle < int(assertion['detail']) and
            int(assertion['value']) == 0x50000000 and int(accepted['value']) == 0x800000000000000b and
            0 <= int(assertion['committed']) <= ordinal < total and mepc % 2 == 0):
        raise ValueError('Invalid external interrupt boundary')
    with (program / 'axi.csv').open(newline='', encoding='utf-8') as stream:
        bus = list(csv.DictReader(stream))
    reads = [r for r in bus if r['channel'] == 'peripheral' and int(r['address']) == 0x50000000]
    ars = [int(r['cycle']) for r in reads if r['event'] == 'AR']
    responses = [int(r['cycle']) for r in reads if r['event'] == 'R']
    if not ars or not responses or not (min(ars) < cycle < min(responses)) or min(responses) < min(ars)+502:
        raise ValueError('No actual AXI read was waiting when the interrupt pin rose')
    with (program / 'inputs.csv').open(newline='', encoding='utf-8') as stream:
        pins = csv.DictReader(stream)
        for sample, row in enumerate(pins):
            if int(row['sample']) != sample or int(row['io_extIntrs'], 0) != int(sample >= 4*cycle):
                raise ValueError('Actual external interrupt input trace differs from the recorded event')
    with (program / 'commits.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    if sum(int(row['cycle']) <= trap_cycle for row in rows) != ordinal:
        raise ValueError('Interrupt boundary does not match actual retirement count')
    (target / 'interrupts.csv').write_text(f'committed,mepc\n{ordinal},{mepc}\n', encoding='utf-8')
    return {'status': 'OBSERVED', 'assert_cycle': cycle, 'accepted_cycle': trap_cycle,
            'committed_before_interrupt': ordinal, 'mepc': mepc,
            'actual_read_response_cycle': min(responses),
            'reference_policy': 'Inject MEIP at the observed precise retirement boundary; Spike has no cycle model. Actual pin timing is retained for RTL/Lean replay.'}


def run(program, spike, verify_writeback=False):
    program, spike = program.resolve(), spike.resolve()
    execution = read(program / 'execution.json')
    if execution.get('signature') != 1 or execution.get('committed', 0) < 1:
        raise ValueError('The original RTL program must complete successfully first')
    commit = subprocess.check_output(['git', '-C', str(spike), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != SPIKE_COMMIT:
        raise ValueError('Use the pinned official Spike v1.1.0 commit ' + SPIKE_COMMIT)
    if subprocess.run(['git', '-C', str(spike), 'diff', '--quiet', 'HEAD', '--']).returncode:
        raise ValueError('Official Spike reference source has local changes')
    lock_path = spike / 'chisel2lean-lock.json'
    if lock_path.exists():
        lock = read(lock_path)
        if lock.get('commit') != SPIKE_COMMIT or not lock.get('commit_logging'):
            raise ValueError('Spike lock differs from the pinned commit/logging configuration')
        for name, expected in lock['files_sha256'].items():
            path = (spike / name).resolve()
            if not path.is_relative_to(spike) or digest(path) != expected:
                raise ValueError('Pinned Spike dependency changed: ' + name)
    target = program / 'isa-comparison'
    target.mkdir(exist_ok=False)
    source = ROOT / 'verification/xiangshan/programs/spike_reference.cpp'
    shutil.copy2(source, target / source.name)
    devices = source.with_name('reference_devices.h')
    shutil.copy2(devices, target / devices.name)
    libraries = [spike / 'build' / ('lib'+name+'.a') for name in ('riscv', 'disasm', 'softfloat', 'fesvr', 'fdt')]
    dependencies = [*libraries, spike / 'build/config.h', source, devices]
    boundary = interrupt_boundary(program, target, execution['committed'])
    if boundary:
        dependencies += [program / name for name in ('environment.json', 'events.csv', 'inputs.csv', 'axi.csv', 'commits.csv')]
    hashes = {str(path): digest(path) for path in dependencies}
    command = ['g++', '-std=c++17', '-O2', '-I'+str(spike / 'build'), '-I'+str(spike),
               '-I'+str(spike / 'softfloat'), source.name, '-Wl,--start-group',
               *map(str, libraries), '-Wl,--end-group', '-ldl', '-pthread',
               '-lboost_regex', '-lboost_system', '-o', 'spike-reference']
    if run_logged(command, target / 'build', cwd=target, echo=False):
        raise RuntimeError('Spike interface adapter did not compile: ' + str(target))
    image = 'segments.csv' if (program / 'segments.csv').exists() else 'program.bin'
    if image == 'segments.csv':
        receipt = read(program / 'elf-image.json')
        if receipt['program_elf_sha256'] != digest(program / 'program.elf'):
            raise ValueError('Program ELF changed')
        shutil.copy2(program / image, target / image)
        for segment in receipt['segments']:
            if digest(program / segment['file']) != segment['sha256']:
                raise ValueError('Staged ELF load image changed')
            shutil.copy2(program / segment['file'], target / segment['file'])
    if run_logged([str(target / 'spike-reference'), str(target / image) if image == 'segments.csv' else str(program / image),
                   str(execution['committed']), *([str(target / 'interrupts.csv')] if boundary else [])],
                  target / 'execution', cwd=target, echo=False):
        raise RuntimeError('Independent Spike execution failed: ' + str(target))
    result = compare(program, target, target)
    if boundary:
        result['external_interrupt_interleaving'] = boundary
    if verify_writeback:
        result['memory_writeback_comparison'] = check_writeback(program, target, target)
    for path, expected in hashes.items():
        if digest(Path(path)) != expected:
            raise ValueError('Reference dependency changed during validation: ' + path)
    result.update(spike_commit=commit, dependencies_sha256=hashes,
                  program_sha256=digest(program / 'program.bin'),
                  program_elf_sha256=digest(program / 'program.elf'),
                  rtl_commits_sha256=digest(program / 'commits.csv'),
                  reference_commits_sha256=digest(target / 'spike-commits.log'),
                  reference_snapshots_sha256=digest(target / 'registers.csv'),
                  reference_executable_sha256=digest(target / 'spike-reference'),
                  reference_lock_sha256=digest(lock_path) if lock_path.exists() else None,
                  reference_length_policy='All actual RTL retirement rows, including completion drain; reference must independently reach signature 1 within them.',
                  initial_conditions={'pc': 0x10000000, 'gprs': 'zero',
                    'memory': 'zero except program image' + (' and explicit test-device value 0x12345678 at 0x50000000' if boundary else '')},
                  scope='Finite program test. GPRs are reconstructed from actual retired RTL writebacks; PC is compared only if recorded.')
    write(target / 'summary.json', result)
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--spike', type=Path, required=True)
    parser.add_argument('--verify-writeback', action='store_true', help='For the write-once cache program, require actual DRAM writes and compare their bytes')
    args = parser.parse_args()
    run(args.program.resolve(), args.spike.resolve(), args.verify_writeback)


if __name__ == '__main__':
    main()
