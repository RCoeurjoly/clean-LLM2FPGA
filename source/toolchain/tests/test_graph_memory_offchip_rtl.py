"""Exercise the emitted caller against completion-driven memory responses."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from compiler.stages import StageError
from scripts.comparison.verify_graph_offchip_rtl import qualify_offchip_caller


class OffchipCallerRTLTests(unittest.TestCase):
    def tools(self):
        return {name: Path(value) for name, value in {
            'memory': os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            'translate': os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL'],
            'calyx': os.environ['LLM2FPGA_CALYX_TOOL'],
            'iverilog': shutil.which('iverilog'), 'vvp': shutil.which('vvp')}.items()}

    def test_variable_responses_and_delayed_retirement_preserve_read_results(self):
        with tempfile.TemporaryDirectory() as directory:
            result = qualify_offchip_caller(Path(directory), self.tools())
            self.assertEqual(len(result['cases']), 3)
            self.assertTrue(all(case['requests'] == 2 and case['result'] == 0xd1
                                for case in result['cases']))

    def test_missing_retirement_cannot_pass_delayed_completion_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(StageError) as caught:
                qualify_offchip_caller(root, self.tools(), retirement=False)
            self.assertEqual(caught.exception.receipt.name, 'case-1')
            self.assertIn('incorrect delayed read result requests=1 value=a7 expected=d1',
                          (root/'case-1/stdout.log').read_text())
            self.assertFalse((root/'qualification.json').exists())

    def test_native_caller_composes_with_shared_bank_and_cdc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = qualify_offchip_caller(root, self.tools(), transport='bank-cdc')
            self.assertEqual(len(result['cases']), 12)
            self.assertTrue(all(case['requests'] == 2 and case['result'] == 0xd1
                                for case in result['cases']))

    def test_composed_transport_rejects_illegal_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(StageError) as caught:
                qualify_offchip_caller(root, self.tools(), transport='bank-cdc', invalid_write=True)
            self.assertEqual(caught.exception.receipt.name, 'case-1-core17-ui6-phase1')
            self.assertIn('DDR transport fault', (root/'case-1-core17-ui6-phase1/stdout.log').read_text())
            self.assertFalse((root/'qualification.json').exists())

    def test_emitted_caller_runs_only_after_streamed_image_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=qualify_offchip_caller(root,self.tools(),transport='bank-loaded-cdc')
            self.assertEqual(len(result['cases']),24)
            self.assertTrue(all(case['requests']==2 and case['result']==0xd1
                                for case in result['cases']))

    def test_emitted_caller_cannot_pass_corrupted_image_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(StageError) as caught:
                qualify_offchip_caller(root,self.tools(),transport='bank-loaded-cdc',corrupt_load=True)
            self.assertIn('DDR load readback fault',(root/caught.exception.receipt.name/'stdout.log').read_text())
            self.assertFalse((root/'qualification.json').exists())

    def test_invalid_qualification_options_invalidate_old_acceptance(self):
        for options in ({'transport':'unsupported'}, {'transport':'peer','invalid_write':True}):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'qualification.json').write_text('{"status":"old-success"}')
                with self.assertRaises(ValueError):
                    qualify_offchip_caller(root, {}, **options)
                self.assertFalse((root/'qualification.json').exists())
                self.assertFalse((root/'caller.mlir').exists())
