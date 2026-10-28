"""Block simulator construction consumes an authenticated initialized design."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


class BlockSimulatorBuilderTests(unittest.TestCase):
    def builder(self):
        name='scripts.comparison.build_graph_block_simulator'
        self.assertIsNotNone(importlib.util.find_spec(name),'authenticated block builder missing')
        return __import__(name,fromlist=['build_block_simulator']).build_block_simulator

    def test_missing_initialized_stage_does_not_create_testbench_or_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'build'
            with self.assertRaisesRegex(ValueError,'stage'):
                self.builder()(root/'core',root/'sv',root/'tb.sv',output,
                    verilator=root/'verilator',make=root/'make')
            self.assertFalse(output.exists())

    def test_output_cannot_overwrite_block_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);core=root/'core';core.mkdir()
            protected=core/'success.json';protected.write_text('preserve input')
            with self.assertRaisesRegex(ValueError,'overlap'):
                self.builder()(core,root/'sv',root/'tb.sv',core,
                    verilator=root/'verilator',make=root/'make')
            self.assertEqual(protected.read_text(),'preserve input')
