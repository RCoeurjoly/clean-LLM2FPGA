"""Index actual server receipts; keep RTL/ISA and complete Lean acceptance separate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from server_bundle import read
from yosys_hierarchy import digest


def collect(work, output):
    cases = []
    for case in sorted((work / 'programs').iterdir()):
        if not (case / 'execution.json').is_file() or not (case / 'isa-comparison/summary.json').is_file():
            continue
        rtl, isa = read(case / 'execution.json'), read(case / 'isa-comparison/summary.json')
        provenance = read(case / 'provenance.json')
        if rtl.get('signature') != 1 or isa.get('status') != 'RTL_SPIKE_PC_GPR_PASS':
            continue
        if digest(case / 'commits.csv') != isa['rtl_commits_sha256'] or digest(case / 'program.bin') != isa['program_sha256']:
            raise ValueError('Completed program/retirement receipt changed: '+str(case))
        lean = []
        paths = sorted(case.glob('lean-retirement*/summary.json'))
        if (case / 'lean-port-replay/summary.json').is_file():
            paths.append(case / 'lean-port-replay/summary.json')
        for path in paths:
            result = read(path)
            entry = {'path': str(path), 'sha256': digest(path), 'status': result['status'],
                     'scope': 'physical ports' if path.parent.name == 'lean-port-replay' else 'physical ports, selected internal wires and actual retirement'}
            for key in ['samples','samples_expected','committed','output_comparisons','wire_comparisons','error']:
                if key in result:
                    entry[key] = result[key]
            progress = path.parent / 'progress.json'
            if result['status'] == 'RUNNING' and progress.exists():
                entry['progress'] = read(progress)
            lean.append(entry)
        source = Path(provenance.get('program_source', 'smoke.S')).name
        cases.append({'case':case.name,'source':source,'dram_text':provenance.get('dram_text',False),
                      'rtl':{**rtl,'receipt':str(case/'execution.json'),'sha256':digest(case/'execution.json')},
                      'isa':{key:isa.get(key,'NOT_RECORDED') for key in ['status','committed_instructions','integer_writebacks','retired_pc_comparison','integer_writeback_comparison',
                          'reconstructed_gprs_after_every_commit','actual_retired_instruction_bits','full_fp_csr_state_comparison']},
                      'isa_receipt':str(case/'isa-comparison/summary.json'), 'isa_receipt_sha256':digest(case/'isa-comparison/summary.json'),
                      'writeback':isa.get('memory_writeback_comparison'),
                      'interrupt_interleaving':isa.get('external_interrupt_interleaving'), 'lean_runs':lean})
    accepted = [case['case'] for case in cases if any(run['status'] == 'RTL_LEAN_RETIREMENT_PASS' for run in case['lean_runs'])]
    model = work / 'model-observed-v4/manifest.json'
    model_info = read(model)
    batches = sorted(work.glob('program-acceptance-batch-*.json'))
    latest_batch = batches[-1] if batches else None
    result = {'status':'PARTIAL_PROGRAM_ACCEPTANCE' if accepted else 'IN_PROGRESS',
              'generated_at_utc':datetime.now(timezone.utc).isoformat(),
              'upstream_commit':'997cd5b35cfbd26586674aa553d0f04b27914cb2','config':'DefaultConfig','cores':1,
              'original_hardware_modified':False,'spike_commit':'530af85d83781a3dae31a4ace84a573ec255fefa',
              'accepted_three_way_program_cases':accepted,'cases':cases,
              'finite_batch_status':read(latest_batch)['status'] if latest_batch else 'NOT_SCHEDULED',
              'finite_batch_receipt':str(latest_batch) if latest_batch else None,
              'observed_model':{'manifest':str(model),'sha256':digest(model),'lean_checked':model_info['lean_checked'],
                                'nodes':model_info['nodes'],'read_only_taps':len(model_info['read_only_observations'])},
              'pending':['Longer realistic programs and retirement workloads','Broader interrupt/event schedules','Broader ISA and privileged-state coverage'],
              'universal_equivalence':'NOT_PROVED',
              'scope':'Finite fixed-configuration tests. RTL/Spike success never implies Lean success; only completed RTL_LEAN_RETIREMENT_PASS receipts count as three-way program acceptance.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Readers can inspect progress while several finite jobs finish independently.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, delete=False) as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        temporary = Path(stream.name)
    os.replace(temporary, output)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    collect(args.work.resolve(), args.out.resolve())
