"""Static context capture is separate from native feedback linking."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import torch
from compiler.generation import capture_contexts


class RuntimeModel(torch.nn.Module):
    def forward(self, tokens):
        return tokens + 1


class RuntimeLogits(torch.nn.Module):
    def forward(self, tokens):
        return torch.stack((tokens, -tokens, tokens), dim=-1)


class ContextCaptureTests(unittest.TestCase):
    def test_generation_step_uses_last_row_and_first_argmax_tie(self):
        from compiler.generation import GenerationStep
        step = GenerationStep(RuntimeLogits())
        for value, wanted in ((9, 0), (-9, 1), (0, 0), ((1 << 63)-1, 0)):
            tokens = torch.tensor([[123, value]], dtype=torch.int64)
            token, logits = step(tokens)
            self.assertEqual(int(token), wanted)
            self.assertTrue(torch.equal(logits, RuntimeLogits()(tokens)[0, -1]))

    def test_generation_context_graphs_retain_runtime_argmax(self):
        from compiler.generation import capture_generation_contexts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('compiler.generation.build_fixed_point_model', return_value=RuntimeLogits()):
                receipts = capture_generation_contexts(object(), tuple(range(4, 20)), root)
            self.assertEqual(len(receipts), 16)
            for length, receipt in zip(range(4, 20), receipts, strict=True):
                self.assertEqual(receipt.user_input_count, 1)
                self.assertEqual(receipt.inputs[0]['shape'], [1, length])
                self.assertIn('aten.argmax.default', receipt.operators)
                exported = torch.export.load(root / f'context-{length:02d}/model.pt2').module()
                for value, wanted in ((9, 0), (-9, 1), (0, 0)):
                    tokens = torch.full((1, length), value, dtype=torch.int64)
                    token, logits = exported(tokens)
                    self.assertEqual(int(token), wanted)
                    self.assertTrue(torch.equal(logits, RuntimeLogits()(tokens)[0, -1]))

    def test_missing_duplicate_or_reordered_contexts_are_rejected_before_capture(self):
        required = tuple(range(4, 20))
        for lengths in (required[:-1], required + (19,), tuple(reversed(required)), (4, 4)):
            with self.subTest(lengths=lengths), tempfile.TemporaryDirectory() as directory:
                with patch('compiler.generation.build_fixed_point_model') as model:
                    with self.assertRaisesRegex(ValueError, 'contexts 4 through 19'):
                        capture_contexts(object(), lengths, Path(directory) / 'captures')
                    model.assert_not_called()
                self.assertFalse((Path(directory) / 'captures').exists())

    def test_unsupported_model_contract_is_rejected_before_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'captures'
            with self.assertRaisesRegex(ValueError, 'unsupported fixed-point geometry'):
                capture_contexts(SimpleNamespace(geometry={}, arithmetic={}),
                                 tuple(range(4, 20)), root)
            self.assertFalse(root.exists())

    def test_each_context_is_captured_as_runtime_tensor_with_distinct_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('compiler.generation.build_fixed_point_model', return_value=RuntimeModel()):
                receipts = capture_contexts(object(), tuple(range(4, 20)), root)
            self.assertEqual(len(receipts), 16)
            self.assertEqual(len({receipt.program_sha256 for receipt in receipts}), 16)
            for length, receipt in zip(range(4, 20), receipts, strict=True):
                self.assertEqual(receipt.user_input_count, 1)
                self.assertEqual(receipt.inputs[0]['shape'], [1, length])
                self.assertEqual(receipt.inputs[0]['dtype'], 'torch.int64')
                self.assertTrue((root / f'context-{length:02d}' / 'model.pt2').is_file())
