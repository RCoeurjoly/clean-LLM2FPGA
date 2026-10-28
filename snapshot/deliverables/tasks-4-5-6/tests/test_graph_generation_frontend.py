"""Generation outputs remain runtime PyTorch computation before lowering."""
import unittest
import torch


class Logits(torch.nn.Module):
    def forward(self, tokens):
        return torch.stack((tokens + 1, tokens * 2, -tokens), dim=-1)


class GenerationFrontendTests(unittest.TestCase):
    def test_exported_step_uses_runtime_last_row_and_first_maximum(self):
        from compiler.generation import GenerationStep
        model = GenerationStep(Logits())
        exported = torch.export.export(model, (torch.tensor([[0, 0, 0, 3]]),))
        for value, token, logits in ((3, 1, [4, 6, -3]), (-4, 2, [-3, -8, 4]),
                                     (1, 0, [2, 2, -1])):
            with self.subTest(value=value):
                inputs = torch.tensor([[9, 8, 7, value]], dtype=torch.int64)
                actual_token, actual_logits = exported.module()(inputs)
                self.assertEqual(int(actual_token), token)
                self.assertEqual(actual_logits.tolist(), logits)

