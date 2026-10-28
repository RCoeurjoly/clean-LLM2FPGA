#!/usr/bin/env python3
"""Retain native integer-lowering evidence, with verified graph input identity."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tool', type=Path, required=True)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--capture-receipt', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    captured = json.loads(args.capture_receipt.read_text())
    if digest(args.raw) != captured['raw_torch_sha256']:
        raise ValueError('raw graph differs from verified capture receipt')
    args.work_dir.mkdir(parents=True, exist_ok=True)
    output = args.work_dir / 'model.integer.linalg.mlir'
    pipeline = ('builtin.module(llm2fpga-lower-exact-integer-ops,llm2fpga-lower-serial-gemv,'
                'torchdynamo-export-to-torch-backend-pipeline,llm2fpga-lower-exact-integer-ops,'
                'torch-backend-to-linalg-on-tensors-backend-pipeline,llm2fpga-check-standard-integer-ir)')
    command = [str(args.tool), str(args.raw), '--pass-pipeline=' + pipeline, '-o', str(output)]
    result = subprocess.run(command, text=True, capture_output=True)
    (args.work_dir / 'integer.stdout.log').write_text(result.stdout)
    (args.work_dir / 'integer.stderr.log').write_text(result.stderr)
    # A failed command cannot overwrite or leave a current acceptance receipt.
    if result.returncode:
        args.receipt.unlink(missing_ok=True)
        raise ValueError(f'native lowering failed ({result.returncode}): {result.stderr}')
    sources = list((ROOT / 'compiler/passes').rglob('*.cpp')) + list((ROOT / 'compiler/passes').rglob('*.h'))
    receipt = {'schema_version': 1, 'stage': 'verified-standard-integer-ir',
               'program_sha256': captured['program_sha256'],
               'raw_sha256': digest(args.raw), 'tool_sha256': digest(args.tool),
               'output_sha256': digest(output), 'pipeline': pipeline,
               'source_hashes': {str(path.relative_to(ROOT)): digest(path) for path in sorted(sources)},
               'scope': 'four-token graph; native boundary verifies standard dialects and integer types; no RTL claim'}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + '\n')
    print('four-token graph reaches verified standard integer IR')


if __name__ == '__main__':
    main()
