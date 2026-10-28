"""Verification preflight rejects stale or wrong-shape step captures."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/comparison'))


class StepVerifierTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory) / 'context-04'
        root.mkdir()
        program = root / 'model.pt2'
        program.write_bytes(b'preflight fixture, not executable PyTorch')
        from compiler.stages import _identity
        receipt = {'graph_sha256': 'graph', 'signature_sha256': 'signature',
                   'state_identities': {}, 'constant_identities': {}, 'range_constraints': {},
                   'user_input_count': 1,
                   'inputs': [{'shape': [1, 4], 'dtype': 'torch.int64'}],
                   'operators': {'aten.argmax.default': 1}}
        receipt['program_sha256'] = _identity({'graph': 'graph', 'signature': 'signature',
            'state': {}, 'constants': {}, 'constraints': {}, 'inputs': receipt['inputs']})
        (root / 'capture.json').write_text(json.dumps(receipt))
        return root, receipt

    def test_authenticated_shape_is_accepted(self):
        from verify_graph_generation_steps import validate_step_capture
        with tempfile.TemporaryDirectory() as directory:
            _, receipt = self.fixture(directory)
            self.assertEqual(validate_step_capture(Path(directory), 4), receipt)

    def test_changed_program_wrong_shape_or_missing_argmax_is_rejected(self):
        from verify_graph_generation_steps import validate_step_capture
        for change in ('program', 'shape', 'argmax', 'runtime_count'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root, receipt = self.fixture(directory)
                if change == 'program':
                    (root / 'model.pt2').unlink()
                elif change == 'shape':
                    receipt['inputs'][0]['shape'] = [1, 5]
                elif change == 'argmax':
                    receipt['operators'] = {}
                else:
                    receipt['user_input_count'] = 0
                (root / 'capture.json').write_text(json.dumps(receipt))
                with self.assertRaises(ValueError):
                    validate_step_capture(Path(directory), 4)

    def test_loaded_export_is_authenticated_and_changed_tensor_is_rejected(self):
        import torch
        from compiler.frontend.capture import capture_model
        from verify_graph_generation_steps import authenticate_export, validate_step_capture
        class Kernel(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer('bias', torch.tensor([0, 1, 0], dtype=torch.int64))
            def forward(self, tokens):
                logits = tokens[0, -1] + self.bias
                return torch.argmax(logits), logits
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture_model(Kernel(), (torch.ones((1, 4), dtype=torch.int64),), root / 'context-04')
            receipt = validate_step_capture(root, 4)
            exported = torch.export.load(root / 'context-04/model.pt2')
            authenticate_export(exported, receipt)
            exported.state_dict['bias'][1] += 1
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                authenticate_export(exported, receipt)
