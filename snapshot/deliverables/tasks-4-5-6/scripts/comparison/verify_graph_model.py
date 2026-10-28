#!/usr/bin/env python3
"""Verification-only access to oracles; compiler modules do not import this."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler.frontend.package_inputs import load_compile_inputs
from verify_closure_reference import _pinned_numpy_reference


def verify_cpu_cases(model, independent, prompts):
    """Compare every signed-64 logit before extending each runtime context."""
    import torch
    if not isinstance(prompts, dict) or not prompts:
        raise ValueError('CPU comparison requires named prompt cases')
    cases = []
    with torch.no_grad():
        for label, prompt in prompts.items():
            if (not isinstance(label, str) or not label or type(prompt) is not list
                    or len(prompt) != 4 or any(type(token) is not int for token in prompt)):
                raise ValueError('invalid CPU comparison prompt')
            context, steps = list(prompt), []
            for index in range(16):
                actual = model(torch.tensor([context], dtype=torch.int64, device='cpu'))[0, -1]
                expected = torch.from_numpy(independent.forward(context)[-1].copy())
                if (actual.device.type != 'cpu' or actual.dtype != torch.int64
                        or expected.dtype != torch.int64 or not torch.equal(actual, expected)):
                    raise ValueError(f'independent integer logits differ: {label} step {index}')
                token = int(torch.argmax(actual))
                steps.append({'step': index, 'context_length': len(context),
                              'integer_logits_equal': True, 'token': token,
                              'logit_words_equal': actual.numel(),
                              'logits_sha256': hashlib.sha256(actual.numpy().astype('<i8').tobytes()).hexdigest()})
                context.append(token)
            cases.append({'case': label, 'prompt': list(prompt), 'steps': steps})
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--reference-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Invalidate prior acceptance before loading model/reference inputs, while
    # keeping an invalid output destination from deleting any of those inputs.
    if args.output.is_symlink():
        raise ValueError('comparison output must not be a symlink')
    output = args.output.resolve()
    if any(output.is_relative_to(path.resolve()) for path in
           (args.snapshot, args.package, args.reference_source)):
        raise ValueError('comparison output overlaps input directory')
    args.output = output
    args.output.unlink(missing_ok=True)
    import torch
    from compiler.frontend.package_model import build_fixed_point_model
    inputs = load_compile_inputs(args.snapshot, args.package,
                                 ROOT / 'compiler/contracts/tinystories-1m-fixed-point.json')
    model = build_fixed_point_model(inputs)
    from TinyStories.model_adapter_exact_package import load_successor_exact_model
    reference_root = ROOT / 'artifacts/reference'
    historical = load_successor_exact_model(reference_root / 'tinystories-1m-exact-input-contract.json',
                                             args.package, args.snapshot,
                                             reference_root / 'tinystories-1m-exact-generation.json').model
    actual_buffers, old_buffers = dict(model.named_buffers()), dict(historical.named_buffers())
    if set(actual_buffers) != set(old_buffers):
        raise ValueError('materialized buffer names differ')
    for name, tensor in actual_buffers.items():
        if not torch.equal(tensor, old_buffers[name]):
            raise ValueError(f'materialized buffer differs: {name}')
    reference_class, identities = _pinned_numpy_reference(args.reference_source)
    independent = reference_class(args.package)
    fixture = json.loads((reference_root / 'tinystories-1m-fixed-point-continuation-oracle.json').read_text())
    context = list(fixture['token_contract']['initial_context'])
    results = []
    with torch.no_grad():
        for index, step in enumerate(fixture['steps']):
            ids = torch.tensor([context], dtype=torch.int64)
            actual = model(ids)[0, -1]
            expected = torch.from_numpy(independent.forward(context)[-1].copy())
            if not torch.equal(actual, expected):
                raise ValueError(f'independent integer logits differ at step {index}')
            token = int(torch.argmax(actual))
            if token != step['selected_token']['value']:
                raise ValueError(f'continuation token differs at step {index}')
            results.append({'step': index, 'context_length': len(context),
                            'integer_logits_equal': True, 'token': token,
                            'logits_sha256': hashlib.sha256(actual.numpy().astype('<i8').tobytes()).hexdigest()})
            context.append(token)
    # Same-shape alternate tokens must remain data, not trace-time constants.
    alternate = torch.tensor([[1, 2, 3, 4]], dtype=torch.int64)
    with torch.no_grad():
        first = model(torch.tensor([fixture['token_contract']['initial_context']], dtype=torch.int64))
        second = model(alternate)
    if torch.equal(first, second):
        raise ValueError('distinct token inputs unexpectedly produced identical logits')
    runtime_cases = verify_cpu_cases(model, independent,
        {'sequential': [1, 2, 3, 4], 'alternate': [50256, 0, 9, 123]})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'schema_version': 1, 'input_hashes': inputs.input_hashes,
                                      'contract_sha256': inputs.contract_sha256,
                                      'reference_sources': identities,
                                      'materialized_buffers_equal': len(actual_buffers),
                                      'runtime_token_inputs_distinct': True,
                                      'runtime_cases': runtime_cases,
                                      'steps': results}, indent=2) + '\n')
    print(f'{len(results)} historical contexts and {len(runtime_cases)} full runtime continuations: '
          f'all integer logits equal; {len(actual_buffers)} buffers equal')


if __name__ == '__main__':
    main()
