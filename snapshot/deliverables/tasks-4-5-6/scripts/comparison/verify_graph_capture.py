#!/usr/bin/env python3
"""Exercise capture/raw import and changed runtime inputs without golden fixtures."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler.frontend.package_inputs import load_compile_inputs
from compiler.frontend.package_model import build_fixed_point_model
from compiler.frontend.capture import capture_model, import_raw_torch, serialize_raw_torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    import torch
    model = build_fixed_point_model(load_compile_inputs(args.snapshot, args.package,
                                   ROOT / 'compiler/contracts/tinystories-1m-fixed-point.json'))
    first, second = torch.tensor([[1, 2, 3, 4]]), torch.tensor([[4, 3, 2, 1]])
    receipt = capture_model(model, (first,), args.work_dir)
    exported = torch.export.load(args.work_dir / 'model.pt2')
    with torch.no_grad():
        for ids in (first, second):
            if not torch.equal(model(ids), exported.module()(ids)):
                raise ValueError('capture eager/export mismatch on runtime input')
    module = import_raw_torch(exported)
    raw_bytes = serialize_raw_torch(module).encode()
    (args.work_dir / 'model.raw.mlir').write_bytes(raw_bytes)
    result = asdict(receipt)
    result.pop('nodes')  # Full node provenance remains in capture.json.
    result.update({'eager_export_equal': True, 'runtime_inputs_checked': 2,
                   'raw_torch_verified': True, 'raw_torch_sha256': hashlib.sha256(raw_bytes).hexdigest(),
                   'lowering_status': 'pending native integer passes'})
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(result, indent=2) + '\n')
    print(f'capture replay and raw Torch import passed: {sum(receipt.operators.values())} nodes')


if __name__ == '__main__':
    main()
