"""The whole-workload verifier cannot accept incomplete or uncompiled results."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GenerationVerifierTests(unittest.TestCase):
    def test_missing_simulator_removes_previous_acceptance_before_reference_work(self):
        script = Path(__file__).resolve().parents[1]/'scripts/comparison/verify_graph_generation_rtl.py'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'result.json';output.write_text('{"accepted":true}')
            argv=[sys.executable,str(script)]
            for name in ('root','binary','snapshot','package','reference-source'):
                argv+=['--'+name,str(root/name)]
            argv+=['--output',str(output)]
            result=subprocess.run(argv,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Compile hardware before',result.stderr)
            self.assertFalse(output.exists())
