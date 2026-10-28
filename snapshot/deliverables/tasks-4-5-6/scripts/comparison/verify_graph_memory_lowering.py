#!/usr/bin/env python3
"""Execute sliced integer graphs through the shared buffer/loop passes.

This verification-only scalar entry uses declared test inputs as parameters.
Expected outputs are computed independently after compilation, never inserted
into compiler IR. Production runtime-input/RTL acceptance is a separate gate.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import torch
from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
from compiler.pipeline import INTEGER_PROFILE, TORCH_INTEGER_PASSES
from compiler.stages import digest


class SliceKernel(torch.nn.Module):
    def __init__(self, values):
        super().__init__()
        self.register_parameter('values', torch.nn.Parameter(
            torch.tensor(values, dtype=torch.int64).reshape(2, 4), requires_grad=False))

    def forward(self):
        return torch.div(self.values[:, 1:3], -3, rounding_mode='floor').sum()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tool', type=Path, required=True)
    parser.add_argument('--runner', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    values = ([1, 2, 3, 4, 5, 6, 7, 8], [-7, -6, -5, -4, 3, 2, 1, 0],
              [100, -100, 128, 9, 0, -129, 257, 0])
    passes = [argument for name, _, arguments, _ in INTEGER_PROFILE
              if name in {'buffers', 'loops', 'structured'} for argument in arguments]
    scalar_pipeline = TORCH_INTEGER_PASSES[:-1] + ',llm2fpga-test-scalar-entry)'
    llvm_passes = ['--convert-scf-to-cf', '--convert-cf-to-llvm', '--convert-math-to-llvm',
                   '--convert-arith-to-llvm', '--convert-index-to-llvm', '--convert-func-to-llvm',
                   '--finalize-memref-to-llvm', '--reconcile-unrealized-casts']
    results = []
    for index, inputs in enumerate(values):
        directory = args.work_dir / str(index)
        directory.mkdir(parents=True, exist_ok=True)
        model = SliceKernel(inputs)
        raw, standard, llvm = (directory / name for name in ('raw.mlir', 'standard.mlir', 'llvm.mlir'))
        raw.write_text(serialize_raw_torch(import_raw_torch(torch.export.export(model, ()))))
        commands = [[str(args.tool), str(raw), '--pass-pipeline=' + scalar_pipeline, '-o', str(standard)],
                    [str(args.tool), str(standard), *passes, *llvm_passes, '-o', str(llvm)],
                    [str(args.runner), str(llvm), '-e', 'main', '--entry-point-result=i64']]
        for stage, command in enumerate(commands):
            completed = subprocess.run(command, text=True, capture_output=True, timeout=120)
            (directory / f'{stage}.stdout.log').write_text(completed.stdout)
            (directory / f'{stage}.stderr.log').write_text(completed.stderr)
            if completed.returncode:
                raise ValueError(f'case {index} stage {stage}: {completed.stderr}')
        actual, eager = int(completed.stdout.strip()), int(model())
        expected = sum(value // -3 for value in (inputs[1], inputs[2], inputs[5], inputs[6]))
        if actual != eager or actual != expected:
            raise ValueError(f'case {index}: compiled={actual}, PyTorch={eager}, integer={expected}')
        results.append({'inputs': inputs, 'actual': actual, 'pytorch': eager, 'integer_reference': expected,
                        'raw_sha256': digest(raw), 'llvm_sha256': digest(llvm)})
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'cases': results,
        'tool_sha256': digest(args.tool), 'runner_sha256': digest(args.runner),
        'script_sha256': digest(Path(__file__)), 'profile_sha256': digest(ROOT / 'compiler/pipeline.py'),
        'scalar_pipeline': scalar_pipeline, 'buffer_and_loop_passes': passes,
        'scope': 'verification-only scalar ABI; no full-model or RTL equivalence claim'}, indent=2) + '\n')
    print(f'{len(results)} sliced-memory cases: PyTorch, LLVM and integer reference match')


if __name__ == '__main__':
    main()
