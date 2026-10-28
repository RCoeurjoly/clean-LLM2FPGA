"""The Nix compilation-data boundary preserves bytes and excludes results."""
import os
from pathlib import Path
import unittest


class CompileDataBoundaryTests(unittest.TestCase):
    def setUp(self):
        for name in ('LLM2FPGA_COMPILE_PACKAGE', 'LLM2FPGA_COMPILE_SNAPSHOT'):
            self.assertTrue(name in os.environ, 'compilation-data boundary not configured: ' + name)
        self.package = Path(os.environ['LLM2FPGA_COMPILE_PACKAGE'])
        self.snapshot = Path(os.environ['LLM2FPGA_COMPILE_SNAPSHOT'])

    def test_only_declared_model_input_files_are_available(self):
        self.assertEqual(sorted(path.name for path in self.package.iterdir()),
                         ['manifest.json', 'scales.bin', 'weights.bin'])
        self.assertEqual(sorted(path.name for path in self.snapshot.iterdir()),
                         ['config.json', 'pytorch_model.bin'])

    def test_real_loader_authenticates_the_isolated_model_bytes(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        source = Path(os.environ.get('LLM2FPGA_COMPILE_SOURCE',
                                     Path(__file__).resolve().parents[1]))
        inputs = load_compile_inputs(self.snapshot, self.package,
                                     source / 'compiler/contracts/tinystories-1m-fixed-point.json')
        inputs.validate()
        self.assertEqual(inputs.geometry['hidden_size'], 64)
        self.assertEqual(inputs.geometry['n_layer'], 8)
        self.assertEqual(inputs.arithmetic['profile'], 'tinystories-1m-integer-v1')
