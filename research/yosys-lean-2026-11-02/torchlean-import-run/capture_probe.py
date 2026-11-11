#!/usr/bin/env python3
"""Exercise the actual Lean-emitted adapter on the frozen artifact and small controls."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import sys
import time
import traceback

import torch
import torch.ao.quantization.fx._decomposed
from minimal_embedding import MinimalEmbedding

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent


class Add(torch.nn.Module):
    def forward(self, x):
        return x + x


class Linear(torch.nn.Module):
    def __init__(self, transpose=False):
        super().__init__()
        weight = torch.tensor([[1., 2., 0., -1.], [.25, 0., 4., 1.], [0., .5, 0., .25]])
        self.weight = torch.nn.Parameter(weight.T.contiguous() if transpose else weight,
                                         requires_grad=False)
        self.bias = torch.nn.Parameter(torch.tensor([.5, -.25, 2.]), requires_grad=False)
        self.transpose = transpose

    def forward(self, x):
        weight = self.weight.transpose(0, 1) if self.transpose else self.weight
        return torch.nn.functional.linear(x, weight, self.bias)


class Round(torch.nn.Module):
    def forward(self, x):
        return torch.round(x)


class Clamp(torch.nn.Module):
    def forward(self, x):
        return torch.clamp(x, -128, 127)


class ChooseQParams(torch.nn.Module):
    def forward(self, x):
        return torch.ops.quantized_decomposed.choose_qparams.tensor(x, -128, 127, 1e-7, torch.int8)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def words(value):
    if value.dtype != torch.float32:
        raise ValueError('Numerical controls currently use Float32 tensors')
    return [int(word) & 0xffffffff for word in value.detach().contiguous().view(torch.int32).reshape(-1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adapter', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=Path(
        '/nix/store/qvsldlg3biy0bj9yy1ys9hxvi9hn8sdp-tinystories-quantized/model.pt2'))
    parser.add_argument('--work', type=Path, default=Path('/tmp/llm2fpga-torchlean-import'))
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    spec = importlib.util.spec_from_file_location('actual_torchlean_adapter', args.adapter)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    report = {'torchlean_commit': 'f37cbc5aca8e79943b0ef0e3d3499ca9719d7c2b',
              'adapter_origin': 'Upstream script evaluated by Lean 4.35.0',
              'adapter_path': str(args.adapter), 'adapter_sha256': digest(args.adapter),
              'model_path': str(args.model), 'model_sha256': digest(args.model),
              'python': platform.python_version(), 'torch': torch.__version__,
              'threads': torch.get_num_threads(), 'require_torch_export': True,
              'whole_model_imported_into_lean': False, 'equivalence_proved': False, 'cases': {}}
    program = torch.export.load(args.model)
    with torch.no_grad():
        output = program.module()(*program.example_inputs[0], **program.example_inputs[1])
    leaves, _ = torch.utils._pytree.tree_flatten(output)
    report['frozen_reference_outputs'] = [
        {'shape': list(value.shape), 'dtype': str(value.dtype),
         'finite': bool(torch.isfinite(value).all()),
         'sha256': hashlib.sha256(value.detach().contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()}
        for value in leaves]

    def capture(name, exported, numerical=False):
        module = exported.module()
        inputs, kwargs = exported.example_inputs
        if kwargs:
            raise ValueError('This probe expects the frozen positional-only interface')
        path = args.work / (name + '.json')
        started = time.monotonic()
        row = {'graph_json': str(path), 'operators': sorted(set(
            str(node.target) for node in exported.graph.nodes if node.op == 'call_function')),
            'input_shapes': [list(value.shape) for value in inputs],
            'input_dtypes': [str(value.dtype) for value in inputs]}
        try:
            with torch.no_grad():
                graph = adapter.export_torchlean_graph_json(module, inputs, str(path),
                                                            require_torch_export=True)
            row.update(status='captured', nodes=len(graph['nodes']), bytes=path.stat().st_size,
                       sha256=digest(path))
            if path.stat().st_size < 1024 * 1024:
                retained = root / 'artifacts' / path.name
                retained.parent.mkdir(exist_ok=True)
                shutil.copyfile(path, retained)
                row['retained_graph'] = str(retained)
            if numerical:
                with torch.no_grad():
                    expected = module(*inputs)
                expected, _ = torch.utils._pytree.tree_flatten(expected)
                request = {'input_words': [words(value) for value in inputs],
                           'expected_output_words': [words(value) for value in expected]}
                request_path = root / 'artifacts' / (name + '-request.json')
                request_path.write_text(json.dumps(request, indent=2) + '\n')
                row['numerical_request'] = str(request_path)
        except Exception as error:
            trace = root / (name + '.traceback.txt')
            trace.write_text(traceback.format_exc())
            row.update(status='rejected', error_type=type(error).__name__, error=str(error),
                       traceback=str(trace))
        row['seconds'] = time.monotonic() - started
        report['cases'][name] = row
        (root / 'capture-report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'case': name, **row}), flush=True)

    capture('whole-model', program)
    example = (torch.tensor([[-0., .5, -1.25, 3.]], dtype=torch.float32),)
    cases = [('add', Add(), example, True), ('linear', Linear(), example, True),
             ('embedding', MinimalEmbedding(), (torch.tensor([[1, 2, 3, 4]], dtype=torch.int64),), False),
             ('frozen-transposed-linear', Linear(transpose=True), example, False),
             ('round', Round(), example, False), ('clamp', Clamp(), example, False),
             ('choose-qparams', ChooseQParams(), example, False)]
    for name, model, inputs, numerical in cases:
        exported = torch.export.export(model.eval(), inputs)
        saved = args.work / (name + '.pt2')
        torch.export.save(exported, saved)
        capture(name, torch.export.load(saved), numerical)
        report['cases'][name]['saved_export'] = str(saved)
        report['cases'][name]['saved_export_sha256'] = digest(saved)
    if digest(args.model) != report['model_sha256']:
        raise RuntimeError('Frozen model artifact changed')
    report['frozen_model_unchanged'] = True
    (root / 'capture-report.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
