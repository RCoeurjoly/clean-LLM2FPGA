"""An isolation rerun must not preserve stale success or overwrite inputs."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_input_isolation.py'

class IsolationReportGateTests(unittest.TestCase):
    def run_verifier(self, root, output):
        return subprocess.run([sys.executable, str(SCRIPT),
            '--snapshot', str(root / 'snapshot'), '--package', str(root / 'package'),
            '--output', str(output)], capture_output=True, text=True, timeout=30)

    def test_missing_input_invalidates_separate_previous_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'report.json'
            output.write_text('{"built_without_expected_files":true}')
            self.assertNotEqual(self.run_verifier(root, output).returncode, 0)
            self.assertFalse(output.exists())

    def test_report_cannot_replace_input_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ('snapshot', 'package'):
                (root / directory).mkdir()
                protected = root / directory / 'config.json'
                protected.write_text('protected input')
                result = self.run_verifier(root, protected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('overlap', result.stderr)
                self.assertEqual(protected.read_text(), 'protected input')
            link = root / 'report.json'
            link.symlink_to(root / 'snapshot/config.json')
            result = self.run_verifier(root, link)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('symlink', result.stderr)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.read_text(), 'protected input')
