import copy
import os
from pathlib import Path
import tempfile
import unittest

import torch
from torchao.quantization import Int8DynamicActivationInt8WeightConfig, quantize_

from model.import_torch import freeze_buffers
from model.quantize import load_model, token_windows


class TorchAOQuantizationTests(unittest.TestCase):
    @torch.no_grad()
    def test_signed_int8_matmul_accumulates_in_int32(self):
        from model.quantize_torchao import int_mm

        values = torch.tensor([[-128, 127], [127, -128]], dtype=torch.int8)
        expected = torch.tensor([[32513, -32512], [-32512, 32513]], dtype=torch.int32)
        torch.testing.assert_close(int_mm(values, values), expected, rtol=0, atol=0)

    @torch.no_grad()
    def test_quantization_matches_version_two_and_preserves_other_layers(self):
        from model.quantize_torchao import quantize

        torch.manual_seed(17)
        model = torch.nn.Sequential(
            torch.nn.Embedding(32, 64), torch.nn.Linear(64, 16),
            torch.nn.LayerNorm(16), torch.nn.Linear(16, 8),
        ).eval()
        reference = copy.deepcopy(model)
        quantize_(reference, Int8DynamicActivationInt8WeightConfig(version=2))
        embeddings = model[0].weight.detach().clone()
        normalization = model[2].weight.detach().clone()
        actual = quantize(model)
        for index in (1, 3):
            self.assertEqual(actual[index].weight.qdata.dtype, torch.int8)
            self.assertEqual(actual[index].weight.scale.shape, (actual[index].out_features, 1))
            torch.testing.assert_close(actual[index].weight.qdata, reference[index].weight.qdata, rtol=0, atol=0)
            torch.testing.assert_close(actual[index].weight.scale, reference[index].weight.scale, rtol=0, atol=0)
        torch.testing.assert_close(actual[0].weight, embeddings, rtol=0, atol=0)
        torch.testing.assert_close(actual[2].weight, normalization, rtol=0, atol=0)
        tokens = torch.tensor([[0, 1, 2, 31]])
        torch.testing.assert_close(actual(tokens), reference(tokens), rtol=0, atol=0)

    @torch.no_grad()
    def test_linear_export_matches_eager_for_dynamic_token_ranges(self):
        from model.quantize_torchao import export_model

        torch.manual_seed(23)
        model = torch.nn.Sequential(torch.nn.Linear(64, 16)).eval()
        quantize_(model, Int8DynamicActivationInt8WeightConfig(version=2))
        values = torch.randn(1, 4, 64)
        values[:, 0] = 0
        values[:, 1] *= 1e-8
        values[:, 2] *= 1000
        expected = model(values)
        program = export_model(model, values)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'linear.pt2'
            torch.export.save(program, path)
            loaded = torch.export.load(path)
            torch.testing.assert_close(loaded.module()(values), expected, rtol=0, atol=0)
            torch.testing.assert_close(freeze_buffers(loaded).module()(values), expected, rtol=0, atol=0)
        self.assertTrue(any(value.dtype == torch.int8 for value in program.state_dict.values()))
        self.assertTrue(any(node.target == torch.ops.aten.mm.default and
                            node.meta['val'].dtype == torch.int32 for node in program.graph.nodes))

    @torch.no_grad()
    def test_full_model_export_matches_eager_exactly(self):
        from model.quantize_torchao import export_model, quantize

        torch.set_num_threads(1)
        tokens = token_windows(Path(os.environ['CALIBRATION']))[-1]
        model = quantize(load_model(os.environ['MODEL_SNAPSHOT']))
        head = model.model.lm_head
        self.assertEqual(head.weight.qdata.dtype, torch.int8)
        self.assertEqual(head.weight.scale.shape, (head.out_features, 1))
        expected = model(tokens)
        program = export_model(model, tokens)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pt2'
            torch.export.save(program, path)
            loaded = torch.export.load(path)
            actual = loaded.module()(tokens)
            frozen = freeze_buffers(loaded).module()(tokens)
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
        torch.testing.assert_close(frozen, expected, rtol=0, atol=0)
        self.assertTrue(torch.isfinite(actual).all())


if __name__ == '__main__':
    unittest.main()
