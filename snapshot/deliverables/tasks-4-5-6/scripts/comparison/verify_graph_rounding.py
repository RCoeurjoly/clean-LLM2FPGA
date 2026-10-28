#!/usr/bin/env python3
"""Export rounding kernels, compile through typed passes, execute matching LLVM."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import torch
from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
from compiler.frontend.fixed_point_ops import round_shift_signed, activation_qdq
from compiler.pipeline import INTEGER_PROFILE, TORCH_INTEGER_PASSES


class RoundKernel(torch.nn.Module):
    def __init__(self, value, shift):
        super().__init__()
        self.register_parameter('value', torch.nn.Parameter(
            torch.tensor(value, dtype=torch.int64), requires_grad=False))
        self.shift = shift

    def forward(self):
        return round_shift_signed(self.value, self.shift)


class QuantizeKernel(torch.nn.Module):
    def __init__(self, value):
        super().__init__()
        self.register_parameter('value', torch.nn.Parameter(
            torch.tensor(value, dtype=torch.int64), requires_grad=False))
        self.register_parameter('scale', torch.nn.Parameter(
            torch.tensor(256, dtype=torch.int64), requires_grad=False))

    def forward(self):
        return activation_qdq(self.value, self.scale)[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tool', type=Path, required=True)
    parser.add_argument('--runner', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    values = [-(1 << 63), (1 << 63)-1, -7, -6, -2, -1, 0, 1, 2, 6, 7]
    for shift in (0, 1, 2, 32, 62):
        for value in values:
            magnitude = abs(value)
            expected = value if shift == 0 else (
                (-1 if value < 0 else 1) * ((magnitude + (1 << (shift-1))) // (1 << shift)))
            cases.append((f'round-{shift}-{value}', RoundKernel(value, shift), expected))
    for value in (-129, -128, -127, -1, 0, 1, 126, 127, 128):
        cases.append((f'quantize-{value}', QuantizeKernel(value), max(-128, min(127, value))))
    results = []
    pipeline = TORCH_INTEGER_PASSES[:-1] + ',llm2fpga-test-scalar-entry)'
    shared_passes = [argument for name, _, arguments, _ in INTEGER_PROFILE
                     if name in {'buffers', 'scratch', 'loops', 'structured'}
                     for argument in arguments]
    for name, model, expected in cases:
        directory = args.work_dir / name
        directory.mkdir(exist_ok=True)
        raw = directory / 'raw.mlir'
        raw.write_text(serialize_raw_torch(import_raw_torch(torch.export.export(model, ()))))
        standard = directory / 'standard.mlir'
        llvm = directory / 'llvm.mlir'
        commands = [
            [str(args.tool), str(raw), '--pass-pipeline=' + pipeline, '-o', str(standard)],
            [str(args.tool), str(standard), *shared_passes, '--convert-scf-to-cf',
             '--convert-cf-to-llvm', '--convert-math-to-llvm', '--convert-arith-to-llvm', '--convert-index-to-llvm',
             '--convert-func-to-llvm', '--finalize-memref-to-llvm', '--reconcile-unrealized-casts',
             '-o', str(llvm)],
            [str(args.runner), str(llvm), '-e', 'main', '--entry-point-result=i64'],
        ]
        for stage, command in enumerate(commands):
            completed = subprocess.run(command, text=True, capture_output=True)
            (directory / f'{stage}.stderr.log').write_text(completed.stderr)
            (directory / f'{stage}.stdout.log').write_text(completed.stdout)
            if completed.returncode:
                raise ValueError(f'{name}: stage {stage} failed: {completed.stderr}')
        actual = int(completed.stdout.strip())
        if actual != expected or int(model()) != expected:
            raise ValueError(f'{name}: compiled {actual}, eager {int(model())}, expected {expected}')
        results.append({'case': name, 'actual': actual, 'expected': expected, 'eager_and_compiled_equal': True})
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version':1, 'cases': results,
        'tool_sha256': hashlib.sha256(args.tool.read_bytes()).hexdigest(),
        'runner_sha256': hashlib.sha256(args.runner.read_bytes()).hexdigest(),
        'pipeline': pipeline, 'shared_passes': shared_passes,
        'profile_sha256': hashlib.sha256((ROOT / 'compiler/pipeline.py').read_bytes()).hexdigest(),
        'verifier_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'scope': 'shared tensor/buffer/loop passes with verification-only scalar ABI; no RTL claim'}, indent=2)+'\n')
    print(f'{len(results)} composed rounding/quantization cases: eager and compiled match')


if __name__ == '__main__':
    main()
