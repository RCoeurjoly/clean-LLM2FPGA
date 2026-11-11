#!/usr/bin/env python3
"""Small saved-export reproducer of TinyStories' first unsupported operator."""

import importlib.util
from pathlib import Path
import sys
import torch


class MinimalEmbedding(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = torch.nn.Embedding(8, 4)
        with torch.no_grad():
            self.embedding.weight.copy_(torch.arange(32, dtype=torch.float32).reshape(8, 4))
        self.requires_grad_(False)

    def forward(self, tokens):
        return self.embedding(tokens)


if __name__ == '__main__':
    adapter_path, output_path = map(Path, sys.argv[1:])
    spec = importlib.util.spec_from_file_location('actual_torchlean_adapter', adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    model = MinimalEmbedding().eval()
    inputs = (torch.tensor([[1, 2, 3, 4]], dtype=torch.int64),)
    saved = output_path.with_suffix('.pt2')
    torch.export.save(torch.export.export(model, inputs), saved)
    restored = torch.export.load(saved)
    adapter.export_torchlean_graph_json(restored.module(), restored.example_inputs[0],
                                       str(output_path), require_torch_export=True)
