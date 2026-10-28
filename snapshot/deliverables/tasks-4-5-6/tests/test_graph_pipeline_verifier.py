"""Composed verification cannot accept incomplete inputs or damage sources."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class PipelineVerifierTests(unittest.TestCase):
    def test_kernel_receipts_require_native_stage_evidence(self):
        source = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_pipeline.py'
        spec = importlib.util.spec_from_file_location('composed_stages', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('task2', 'gemv', 'argmax'):
                with self.subTest(kernel=name), self.assertRaisesRegex(ValueError, 'stage'):
                    module.authenticate_kernel_stages(root, {'kernels': [
                        {'kernel': name, 'stages': [{'identity_sha256': 'invented'}]}]})

    def test_empty_evidence_cannot_satisfy_any_mandatory_check(self):
        source = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_pipeline.py'
        spec = importlib.util.spec_from_file_location('composed_evidence', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in ('kernels', 'rounding', 'contexts', 'block', 'loaded'):
            with self.subTest(check=name), self.assertRaises(ValueError):
                module.validate_check_evidence(name, {}, 'core-identity')

    def invoke(self, core, inputs, output):
        source = Path(__file__).resolve().parents[1] / 'scripts/comparison/verify_graph_pipeline.py'
        spec = importlib.util.spec_from_file_location('composed_verifier', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.verify_graph_pipeline(core, inputs, output)

    def test_incomplete_manifest_invalidates_old_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs.json'
            inputs.write_text(json.dumps({'schema_version': 1, 'skip_full_model': True}))
            output = root / 'verification'
            output.mkdir()
            report = output / 'verification.json'
            report.write_text('old acceptance')
            with self.assertRaisesRegex(ValueError, 'manifest'):
                self.invoke(root / 'core', inputs, output)
            self.assertFalse(report.exists())

    def test_output_cannot_overwrite_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / 'core'
            core.mkdir()
            report = core / 'verification.json'
            report.write_text('protected input')
            with self.assertRaisesRegex(ValueError, 'overlap'):
                self.invoke(core, root / 'inputs.json', core)
            self.assertEqual(report.read_text(), 'protected input')

    def test_malformed_json_invalidates_old_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs.json'
            inputs.write_text('{')
            output = root / 'verification'
            output.mkdir()
            report = output / 'verification.json'
            report.write_text('old acceptance')
            with self.assertRaises(ValueError):
                self.invoke(root / 'core', inputs, output)
            self.assertFalse(report.exists())

    def test_non_object_manifest_invalidates_old_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / 'inputs.json'
            inputs.write_text('null')
            output = root / 'verification'
            output.mkdir()
            report = output / 'verification.json'
            report.write_text('old acceptance')
            with self.assertRaisesRegex(ValueError, 'manifest'):
                self.invoke(root / 'core', inputs, output)
            self.assertFalse(report.exists())
