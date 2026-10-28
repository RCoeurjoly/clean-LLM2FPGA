"""CPU comparison must invalidate stale evidence while preserving its inputs."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest


class ModelReferenceGateTests(unittest.TestCase):
    def load_gate(self):
        directory = Path(__file__).resolve().parents[1]/'scripts/comparison'
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('model_reference_gate', directory/'verify_graph_model.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(directory))

    def cpu_fixture(self, corrupt=False):
        import numpy as np
        import torch
        class Model:
            def __call__(self, ids):
                context = ids.tolist()[0]
                rows = [[token*17+column*3 for column in range(5)] for token in context]
                # Corrupt a nonwinning logit only during the second case.
                if corrupt and context[:4] == [4, 0, 2, 1] and len(context) == 7:
                    rows[-1][1] += 1
                return torch.tensor([rows], dtype=torch.int64)
        class Reference:
            def forward(self, context):
                return np.array(context, dtype=np.int64)[:, None]*17+np.arange(5, dtype=np.int64)[None, :]*3
        return Model(), Reference()

    def test_every_step_and_logit_of_both_cpu_cases_is_checked(self):
        gate = self.load_gate()
        model, reference = self.cpu_fixture()
        cases = gate.verify_cpu_cases(model, reference,
            {'first': [1, 2, 3, 4], 'second': [4, 0, 2, 1]})
        self.assertEqual([case['case'] for case in cases], ['first', 'second'])
        for case in cases:
            self.assertEqual(len(case['steps']), 16)
            self.assertEqual([step['context_length'] for step in case['steps']], list(range(4, 20)))
            self.assertTrue(all(step['integer_logits_equal'] for step in case['steps']))

    def test_nonwinning_logit_mismatch_in_later_second_step_is_rejected(self):
        gate = self.load_gate()
        model, reference = self.cpu_fixture(corrupt=True)
        with self.assertRaisesRegex(ValueError, 'second.*step 3'):
            gate.verify_cpu_cases(model, reference,
                {'first': [1, 2, 3, 4], 'second': [4, 0, 2, 1]})

    def test_equal_values_in_float_logits_do_not_satisfy_integer_contract(self):
        import torch
        gate = self.load_gate()
        model, reference = self.cpu_fixture()
        with self.assertRaisesRegex(ValueError, 'integer logits'):
            gate.verify_cpu_cases(lambda ids: model(ids).to(torch.float64),
                                  reference, {'first': [1, 2, 3, 4]})

    def invoke(self, root, output):
        driver = Path(__file__).resolve().parents[1]/'scripts/comparison/verify_graph_model.py'
        return subprocess.run([sys.executable, str(driver),
            '--snapshot', str(root/'snapshot'), '--package', str(root/'package'),
            '--reference-source', str(root/'reference'), '--output', str(output)],
            capture_output=True, text=True, timeout=30)

    def test_missing_input_removes_previous_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root/'comparison.json'
            output.write_text('{"previous_success": true}')
            result = self.invoke(root, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists(), 'failed verification left stale acceptance')

    def test_output_inside_input_is_rejected_without_deleting_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role in ('snapshot', 'package', 'reference'):
                with self.subTest(role=role):
                    (root/role).mkdir(exist_ok=True)
                    output = root/role/'manifest.json'
                    output.write_text('preserve input bytes')
                    result = self.invoke(root, output)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('overlap', result.stderr)
                    self.assertEqual(output.read_text(), 'preserve input bytes')

    def test_output_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'unowned.txt'
            target.write_text('preserve target bytes')
            output = root/'comparison.json'
            output.symlink_to(target)
            result = self.invoke(root, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('symlink', result.stderr)
            self.assertEqual(target.read_text(), 'preserve target bytes')


if __name__ == '__main__':
    unittest.main()
