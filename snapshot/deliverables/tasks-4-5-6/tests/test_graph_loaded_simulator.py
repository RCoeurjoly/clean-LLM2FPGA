"""The composed DDR simulator must preserve inputs and reject stale profiles."""
import importlib.util
from dataclasses import replace
from pathlib import Path
import unittest
import test_graph_build_core as core_tests


class LoadedSimulatorBoundaryTests(unittest.TestCase):
    setUp = core_tests.BuildCoreBoundaryTests.setUp

    def builder(self):
        name='scripts.comparison.build_graph_loaded_generation_simulator'
        self.assertIsNotNone(importlib.util.find_spec(name),
                             'authenticated loaded simulator builder is missing')
        return __import__(name,fromlist=['build_loaded_generation_simulator']).build_loaded_generation_simulator

    def build(self, output, profile=None):
        return self.builder()(self.output,profile or self.profile,self.root/'transport',
            output,verilator=self.root/'verilator',make=self.root/'make')

    def test_output_overlap_preserves_compilation_input(self):
        self.output.mkdir()
        protected=self.output/'success.json';protected.write_text('preserve input')
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.build(self.output)
        self.assertEqual(protected.read_text(),'preserve input')

    def test_bram_profile_cannot_reuse_loaded_simulator_acceptance(self):
        output=self.root/'simulator';output.mkdir()
        old=output/'success.json';old.write_text('old acceptance')
        with self.assertRaisesRegex(ValueError,'off-chip'):
            self.build(output)
        self.assertFalse(old.exists())
        self.assertFalse((output/'tb.sv').exists())

    def test_stale_profile_rejected_before_testbench_or_native_execution(self):
        profile=replace(self.profile,offchip_immutable=True,narrow_immutable=False)
        (self.root/'memory').write_text('changed compiler')
        output=self.root/'simulator';output.mkdir()
        old=output/'success.json';old.write_text('old acceptance')
        with self.assertRaisesRegex(ValueError,'profile dependency changed'):
            self.build(output,profile)
        self.assertFalse(old.exists())
        self.assertFalse((output/'tb.sv').exists())

    def test_cli_missing_profile_invalidates_separate_old_acceptance(self):
        self.builder()
        from scripts.comparison.build_graph_loaded_generation_simulator import main
        output=self.root/'simulator';output.mkdir()
        old=output/'success.json';old.write_text('old acceptance')
        arguments=[]
        for key,value in {'core':self.output,'transport':self.root/'transport',
                          'output':output,'verilator':self.root/'verilator',
                          'make':self.root/'make','numerical-contract':self.profile.numerical_contract,
                          'calyx-library':self.profile.calyx_library}.items():
            arguments+=['--'+key,str(value)]
        with self.assertRaises(FileNotFoundError):
            main(arguments)
        self.assertFalse(old.exists())
