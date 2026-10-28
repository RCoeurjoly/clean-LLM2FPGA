"""RTL verification must preserve input/build evidence on rejected output paths."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GenerationReferenceGateTests(unittest.TestCase):
    def invoke(self, root, output, *, synthesis=False):
        driver = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_generation_rtl.py'
        argv = [sys.executable, str(driver)]
        for role in ('root', 'binary', 'snapshot', 'package', 'reference-source', 'build-dir'):
            argv.extend(('--' + role, str(root / role)))
        argv.extend(('--output', str(output)))
        if synthesis:
            argv.extend(('--synthesis-root', str(root/'synthesis-root')))
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_output_inside_input_or_build_is_rejected_without_deleting_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role in ('root', 'snapshot', 'package', 'reference-source', 'build-dir'):
                with self.subTest(role=role):
                    (root / role).mkdir()
                    evidence = root / role / 'success.json'
                    evidence.write_text('preserve authenticated evidence')
                    result = self.invoke(root, evidence)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('overlap', result.stderr)
                    self.assertEqual(evidence.read_text(), 'preserve authenticated evidence')

    def test_output_cannot_delete_the_simulator_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'binary'
            binary.write_bytes(b'preserve executable')
            result = self.invoke(root, binary)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('overlap', result.stderr)
            self.assertEqual(binary.read_bytes(), b'preserve executable')

    def test_output_inside_synthesis_cannot_delete_export_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root/'synthesis-root'/'core-synthesis.json'
            evidence.parent.mkdir()
            evidence.write_text('preserve synthesis evidence')
            result = self.invoke(root, evidence, synthesis=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('overlap', result.stderr)
            self.assertEqual(evidence.read_text(), 'preserve synthesis evidence')

    def test_output_symlink_is_rejected_before_unlinking_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.write_text('preserve target')
            output = root / 'comparison.json'
            output.symlink_to(target)
            result = self.invoke(root, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('symlink', result.stderr)
            self.assertTrue(output.is_symlink())
            self.assertEqual(target.read_text(), 'preserve target')

    def test_failed_verification_invalidates_a_separate_previous_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'comparison.json'
            output.write_text('{"previous_success": true}')
            result = self.invoke(root, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
