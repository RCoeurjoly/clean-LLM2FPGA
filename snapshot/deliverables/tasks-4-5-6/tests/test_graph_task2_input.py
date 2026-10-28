"""The standalone deliverable retains the original Task 2 PyTorch computation."""
import hashlib
import json
from pathlib import Path
import unittest


class Task2InputTests(unittest.TestCase):
    def test_packaged_module_is_byte_identical_to_original_and_manifest(self):
        root = Path(__file__).resolve().parents[1]
        packaged = root / 'model_inputs/task2/matmul.py'
        original = root.parents[1] / 'src/matmul.py'
        self.assertTrue(packaged.is_file(), 'standalone Task 2 computation input missing')
        self.assertEqual(packaged.read_bytes(), original.read_bytes())
        manifest = json.loads((packaged.parent / 'provenance.json').read_text())
        self.assertEqual(manifest['sha256'], hashlib.sha256(packaged.read_bytes()).hexdigest())
        self.assertEqual(manifest['original_path'], 'src/matmul.py')
