#!/usr/bin/env python3
"""Verification-only CPU replay; never a compiler or hardware dependency."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

D = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(D))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_step_capture(root, length):
    directory = Path(root) / f'context-{length:02d}'
    receipt = json.loads((directory / 'capture.json').read_text())
    identity = {'graph': receipt['graph_sha256'], 'signature': receipt['signature_sha256'],
                'state': receipt['state_identities'], 'constants': receipt['constant_identities'],
                'constraints': receipt['range_constraints'], 'inputs': receipt['inputs']}
    from compiler.stages import _identity
    if (not (directory / 'model.pt2').is_file() or
            receipt['program_sha256'] != _identity(identity)):
        raise ValueError(f'context {length}: capture identity changed')
    if (receipt['user_input_count'] != 1 or len(receipt['inputs']) != 1 or
            receipt['inputs'][0]['shape'] != [1, length] or
            receipt['inputs'][0]['dtype'] != 'torch.int64' or
            receipt['operators'].get('aten.argmax.default') != 1):
        raise ValueError(f'context {length}: generation-step signature changed')
    return receipt


def authenticate_export(exported, receipt):
    from compiler.frontend.capture import _hash_bytes, _tensor_identity
    if (_hash_bytes(exported.graph_module.code.encode()) != receipt['graph_sha256'] or
            _hash_bytes(str(exported.graph_signature).encode()) != receipt['signature_sha256'] or
            {name: _tensor_identity(tensor) for name, tensor in exported.state_dict.items()} != receipt['state_identities'] or
            {name: _tensor_identity(tensor) for name, tensor in exported.constants.items()} != receipt['constant_identities'] or
            {str(key): str(value) for key, value in exported.range_constraints.items()} != receipt['range_constraints']):
        raise ValueError('loaded export computation/state identity changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('captures', 'snapshot', 'package', 'reference-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.unlink(missing_ok=True)
    captures = {length: validate_step_capture(args.captures, length)
                for length in range(4, 20)}
    import numpy as np
    import torch
    from compiler.frontend.package_inputs import load_compile_inputs
    from compiler.frontend.package_model import build_fixed_point_model
    from compiler.generation import GenerationStep
    from verify_closure_reference import _pinned_numpy_reference
    inputs = load_compile_inputs(args.snapshot, args.package,
                                 D / 'compiler/contracts/tinystories-1m-fixed-point.json')
    eager = GenerationStep(build_fixed_point_model(inputs))
    cls, identities = _pinned_numpy_reference(args.reference_source)
    reference = cls(args.package)
    contexts = {'sequential': [1, 2, 3, 4], 'alternate': [50256, 0, 9, 123]}
    comparisons = []
    with torch.no_grad():
        for length in range(4, 20):
            archive = args.captures / f'context-{length:02d}/model.pt2'
            archive_hash = digest(archive)
            program = torch.export.load(archive)
            authenticate_export(program, captures[length])
            if digest(archive) != archive_hash:
                raise ValueError('export archive changed during loading')
            exported = program.module()
            for label, context in contexts.items():
                assert len(context) == length
                ids = torch.tensor([context], dtype=torch.int64)
                token, logits = exported(ids)
                eager_token, eager_logits = eager(ids)
                expected = reference.forward(context)[-1].copy()
                if (token.dtype != torch.int64 or token.ndim != 0 or
                        logits.dtype != torch.int64 or list(logits.shape) != [50257]):
                    raise ValueError(f'{label} context {length}: output signature changed')
                if (not torch.equal(logits, eager_logits) or
                        not np.array_equal(logits.numpy(), expected) or
                        int(token) != int(eager_token) or int(token) != int(np.argmax(expected))):
                    raise ValueError(f'{label} context {length}: step outputs differ')
                comparisons.append({'case': label, 'context_length': length,
                                    'token': int(token), 'logit_words_equal': 50257,
                                    'logits_sha256': hashlib.sha256(expected.astype('<i8').tobytes()).hexdigest(),
                                    'program_identity_sha256': captures[length]['program_sha256'],
                                    'export_archive_sha256': archive_hash})
                # CPU feedback is checked here; native RTL linking is separate.
                context.append(int(token))
                print(label, length, 'all logits and selected token match both CPU references', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'schema_version': 1, 'comparisons': comparisons,
        'reference_sources': identities, 'input_hashes': inputs.input_hashes,
        'verifier_sha256': digest(Path(__file__)),
        'scope': 'Two CPU continuations across all sixteen exported PyTorch step graphs. Not native feedback linking, RTL equivalence, or board acceptance.'}, indent=2) + '\n')


if __name__ == '__main__':
    main()
