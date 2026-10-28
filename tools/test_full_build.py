"""Checks that a full build cannot silently fall back to archived intermediates."""
import tempfile
import unittest
from pathlib import Path
from tools.full_build import check_generated_inputs


class GeneratedInputsTest(unittest.TestCase):
    def test_missing_generated_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                check_generated_inputs(Path(directory), {'controller/loops.mlir': '0' * 64})

    def test_changed_generated_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'loops.mlir').write_text('changed computation')
            with self.assertRaises(ValueError):
                check_generated_inputs(root, {'loops.mlir': '0' * 64})

    def test_matching_generated_input_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'loops.mlir').write_bytes(b'')
            check_generated_inputs(root, {'loops.mlir':
                'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'})

class RuntimeInputsTest(unittest.TestCase):
    def test_missing_runtime_path_is_rejected_before_build(self):
        from tools.full_build import require_runtime_paths
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                require_runtime_paths([str(Path(directory) / 'missing-runtime')])

    def test_flow_uses_only_prior_generated_outputs(self):
        import json
        root = Path(__file__).resolve().parents[1]
        flow = json.loads((root / 'pipeline/flow.json').read_text())
        paths = json.loads((root / 'toolchain/paths.json').read_text())
        self.assertNotIn(flow['core_store_path'], paths)
        produced = {'core/' + name: identity for name, identity
                    in flow['core']['expected_outputs'].items()}
        for stage in flow['stages']:
            for name, identity in stage['generated_inputs'].items():
                self.assertEqual(produced[name], identity, name)
            produced.update({'tree/' + stage['directory'] + '/' + name: identity
                             for name, identity in stage['expected_outputs'].items()})


if __name__ == '__main__':
    unittest.main()
