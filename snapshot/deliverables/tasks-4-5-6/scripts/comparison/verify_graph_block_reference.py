#!/usr/bin/env python3
"""Compare the production block method with the pinned independent NumPy math.

Verification only: neither this module nor its reference sources are compiler
dependencies. This checks complete block outputs, not intermediate RTL signals.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler.frontend.package_inputs import load_compile_inputs
from compiler.frontend.package_model import build_fixed_point_model
from verify_closure_reference import _pinned_numpy_reference


def reference_block(reference, math, values, layer):
    block, source = f'blocks.{layer}', f'transformer.h.{layer}'
    normalized = math.fixed_layer_norm(
        values, reference.parameters[f'{block}.ln1.weight'],
        reference.parameters[f'{block}.ln1.bias'])
    q, k, v = (reference._gemv(
        normalized, f'{block}.attn.{key}.weight', None,
        f'{source}.attn.attention.{key}_proj') for key in ('q', 'k', 'v'))
    attention = reference._gemv(
        reference._attention(q, k, v), f'{block}.attn.out.weight',
        f'{block}.attn.out.bias', f'{source}.attn.attention.out_proj')
    residual = values + attention
    normalized = math.fixed_layer_norm(
        residual, reference.parameters[f'{block}.ln2.weight'],
        reference.parameters[f'{block}.ln2.bias'])
    hidden = reference._gemv(
        normalized, f'{block}.mlp.fc.weight', f'{block}.mlp.fc.bias',
        f'{source}.mlp.c_fc')
    projected = reference._gemv(
        math.fixed_gelu(hidden), f'{block}.mlp.proj.weight',
        f'{block}.mlp.proj.bias', f'{source}.mlp.c_proj')
    return residual + projected


def main():
    parser = argparse.ArgumentParser()
    for name in ('snapshot', 'package', 'reference-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    import numpy as np
    import torch
    inputs = load_compile_inputs(
        args.snapshot, args.package,
        ROOT / 'compiler/contracts/tinystories-1m-fixed-point.json')
    model = build_fixed_point_model(inputs)
    reference_class, identities = _pinned_numpy_reference(args.reference_source)
    reference = reference_class(args.package)
    math = importlib.import_module(reference_class.__module__)
    comparisons = []
    for tokens in ([1], [1, 2, 3, 4], [50256, 0, *range(1, 18)]):
        ids = torch.tensor([tokens], dtype=torch.int64)
        positions = torch.arange(len(tokens), dtype=torch.int64).unsqueeze(0)
        expected = reference._embedding('token_embedding.weight', np.asarray(tokens))
        expected = expected + reference._embedding(
            'position_embedding.weight', np.arange(len(tokens)))
        with torch.no_grad():
            actual = model._embedding('token_embedding.weight', ids)
            actual = actual + model._embedding('position_embedding.weight', positions)
            for layer in range(8):
                expected = reference_block(reference, math, expected, layer)
                actual = model._block(actual, layer)
                wanted = torch.from_numpy(expected.copy()).unsqueeze(0)
                if not torch.equal(actual, wanted):
                    raise ValueError(f'block {layer}, context {len(tokens)} differs from NumPy')
                comparisons.append({
                    'context_length': len(tokens), 'layer': layer,
                    'checkpoint': f'block.{layer}.output',
                    'complete_output_equal': True, 'shape': list(actual.shape),
                    'output_sha256': hashlib.sha256(
                        actual.numpy().astype('<i8').tobytes()).hexdigest()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sources = (
        'compiler/frontend/package_inputs.py',
        'compiler/frontend/package_model.py',
        'compiler/frontend/fixed_point_ops.py',
        'TinyStories/serial_gemv_boundary.py',
        'scripts/comparison/verify_closure_reference.py',
        'scripts/comparison/verify_graph_block_reference.py',
    )
    args.output.write_text(json.dumps({
        'schema_version': 1, 'input_hashes': inputs.input_hashes,
        'contract_sha256': inputs.contract_sha256,
        'source_hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in sources},
        'torch_version': torch.__version__, 'numpy_version': np.__version__,
        'reference_sources': identities, 'comparisons': comparisons,
        'scope': '24 complete CPU block outputs; no RTL or board acceptance',
    }, indent=2) + '\n')
    print(f'{len(comparisons)} complete production block outputs match pinned NumPy')


if __name__ == '__main__':
    main()
