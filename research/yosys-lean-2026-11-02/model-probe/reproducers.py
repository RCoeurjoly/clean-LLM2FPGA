#!/usr/bin/env python3
"""Small frontend failures, plus a supported control, using the same adapter.

These are minimized feature reproducers, not replacements for the original
model-shaped regressions in probe_model.py. No Lean checker runs here.
"""

import importlib.util
import json
from pathlib import Path
import tempfile

import torch
import torch.ao.quantization.fx._decomposed


class Function(torch.nn.Module):
    def __init__(self, function):
        super().__init__()
        self.function = function

    def forward(self, *arguments):
        return self.function(*arguments)


if __name__ == '__main__':
    root = Path(__file__).parent
    spec = importlib.util.spec_from_file_location('capture_adapter', root / 'capture_adapter.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    torch.set_num_threads(1)
    torch.manual_seed(0)
    values = torch.randn(2, 4)
    cases = [
        ('supported_add', lambda x: x + x, (values,)),
        ('embedding', lambda w, i: torch.nn.functional.embedding(i, w),
         (torch.randn(8, 4), torch.tensor([[0, 2]], dtype=torch.int64))),
        ('dynamic_qparams', lambda x: torch.ops.quantized_decomposed.choose_qparams.tensor(
            x, -128, 127, 0.000244140625, torch.int8), (values,)),
        ('round', lambda x: torch.round(x), (values,)),
        ('clamp', lambda x: torch.clamp(x, -128, 127), (torch.tensor([-200, 3, 200]),)),
        ('where', lambda c, x, y: torch.where(c, x, y),
         (values > 0, values, torch.zeros_like(values))),
        ('computed_linear_weight', lambda x, w: torch.nn.functional.linear(x, w.transpose(0, -1)),
         (values, torch.randn(4, 3))),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix='torchlean-repro-') as temporary:
        for name, function, arguments in cases:
            row = {'name': name, 'inputs': [{'shape': list(x.shape), 'dtype': str(x.dtype)}
                                          for x in arguments]}
            try:
                artifact = adapter.export_torchlean_graph_json(Function(function), arguments,
                    str(Path(temporary) / (name + '.json')), require_torch_export=True)
                row.update(status='captured', nodes=len(artifact['nodes']))
            except Exception as error:
                row.update(status='failed', error_type=type(error).__name__, error=str(error))
            results.append(row)
    (root / 'results' / 'minimal-reproducers.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))
