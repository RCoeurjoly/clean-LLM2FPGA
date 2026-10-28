"""Block comparison must authenticate hardware before constructing references."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BlockAuthenticationGateTests(unittest.TestCase):
    def run_verifier(self,root,report):
        script=Path(__file__).resolve().parents[1]/'scripts/comparison/verify_graph_block_rtl.py'
        arguments=[sys.executable,str(script)]
        for name,value in {'root':root/'core','binary':Path('/usr/bin/true'),
                           'snapshot':root/'snapshot','package':root/'package',
                           'reference-source':root/'reference','output':report}.items():
            arguments+=['--'+name,str(value)]
        return subprocess.run(arguments,text=True,capture_output=True,timeout=30)

    def test_unqualified_executable_is_rejected_before_reference_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);report=root/'report.json';report.write_text('old acceptance')
            result=self.run_verifier(root,report)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('authenticated block simulator',result.stderr)
            self.assertFalse(report.exists())

    def test_report_cannot_delete_block_binding_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'core').mkdir()
            report=root/'core/bindings-reused.json';report.write_text('preserve binding input')
            result=self.run_verifier(root,report)
            self.assertNotEqual(result.returncode,0)
            self.assertTrue(report.exists())
            self.assertEqual(report.read_text(),'preserve binding input')
