import os
from pathlib import Path
import tempfile
import unittest

import torch

from model.quantize import load_model, quantize, token_windows
from model.import_torch import freeze_buffers


class QuantizationTests(unittest.TestCase):
    @torch.no_grad()
    def test_exported_inference_is_exact(self):
        torch.set_num_threads(1)
        windows = token_windows(Path(os.environ['CALIBRATION']))
        model = quantize(load_model(os.environ['MODEL_SNAPSHOT']), windows[:-1])
        tokens = windows[-1]
        expected = model(tokens)
        exported = torch.export.export(model, (tokens,), strict=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.pt2'
            torch.export.save(exported, path)
            actual = torch.export.load(path).module()(tokens)
            frozen = freeze_buffers(torch.export.load(path)).module()(tokens)
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
        torch.testing.assert_close(frozen, expected, rtol=0, atol=0)
        self.assertTrue(torch.isfinite(actual).all())


if __name__ == '__main__':
    unittest.main()
