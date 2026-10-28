"""The composed fixture uses production Q/DQ arithmetic, not expected answers."""
import importlib.util
from pathlib import Path
import unittest
import torch


class ComposedFixtureTests(unittest.TestCase):
    def test_qdq_gemv_matches_independent_scalar_integer_reference(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_initialized_rtl.py'
        spec = importlib.util.spec_from_file_location('initialized_verifier', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model = module.ComposedQuantizedGemv()
        for values in ([65536, -131072, 196608], [128, -256, 512],
                       [-(1 << 31), (1 << 31) - 1, -257]):
            dequantized = []
            for value, scale in zip(values, [65536, 131072, 262144]):
                numerator = value * 256
                code = (abs(numerator) + scale // 2) // scale
                code = max(-128, min(127, -code if numerator < 0 else code))
                product = code * scale
                rounded = (abs(product) + 128) // 256
                dequantized.append(-rounded if product < 0 else rounded)
            expected = sum(a * b for a, b in zip(dequantized, [-3, 5, 2]))
            self.assertEqual(int(model(torch.tensor([values], dtype=torch.int64))), expected)


if __name__ == '__main__':
    unittest.main()
