"""Compiler inputs have no dependency on expected model answers."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace


class InputBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.snapshot = self.root / 'snapshot'
        self.package = self.root / 'package'
        self.snapshot.mkdir()
        self.package.mkdir()
        (self.snapshot / 'config.json').write_text('{"model_type":"gpt_neo"}')
        (self.snapshot / 'pytorch_model.bin').write_bytes(b'snapshot weights')
        (self.package / 'weights.bin').write_bytes(b'\x01\x02')
        (self.package / 'scales.bin').write_bytes(b'scales')
        (self.package / 'manifest.json').write_text(json.dumps({
            'model': {'model_type': 'gpt_neo'}, 'tensors': {},
            'activation_scales': {},
        }))
        self.contract = self.root / 'numerical.json'
        identities = {}
        for kind, directory, names in (
            ('snapshot', self.snapshot, ('config.json', 'pytorch_model.bin')),
            ('package', self.package, ('manifest.json', 'weights.bin', 'scales.bin')),
        ):
            identities[kind] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                for name in names}
        self.contract.write_text(json.dumps({'schema_version': 1, 'inputs': identities,
                                           'arithmetic': {'value_fraction_bits': 16}}))

    def test_model_inputs_without_expected_files(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        inputs = load_compile_inputs(self.snapshot, self.package, self.contract)
        self.assertEqual(inputs.weights, b'\x01\x02')
        self.assertEqual(inputs.geometry['model_type'], 'gpt_neo')
        self.assertFalse((self.package / 'quality.json').exists())
        self.assertEqual(set(inputs.input_hashes['package']),
                         {'manifest.json', 'weights.bin', 'scales.bin'})

    def test_missing_or_altered_weight_is_error(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        (self.package / 'weights.bin').write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            load_compile_inputs(self.snapshot, self.package, self.contract)
        (self.package / 'weights.bin').unlink()
        with self.assertRaisesRegex(ValueError, 'missing compiler input'):
            load_compile_inputs(self.snapshot, self.package, self.contract)

    def test_expected_file_cannot_be_added_to_compiler_inputs(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        contract = json.loads(self.contract.read_text())
        contract['inputs']['package']['quality.json'] = '0' * 64
        self.contract.write_text(json.dumps(contract))
        with self.assertRaisesRegex(ValueError, 'input allowlist'):
            load_compile_inputs(self.snapshot, self.package, self.contract)

    def test_materialized_weights_cannot_change_after_loading(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        inputs = load_compile_inputs(self.snapshot, self.package, self.contract)
        self.assertTrue(hasattr(inputs, 'validate'), 'loaded inputs need revalidation before compilation')
        changed = replace(inputs, weights=b'changed after authentication')
        with self.assertRaisesRegex(ValueError, 'materialized compiler inputs changed'):
            changed.validate()

    def test_materialized_manifest_cannot_change_after_loading(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        inputs = load_compile_inputs(self.snapshot, self.package, self.contract)
        self.assertTrue(hasattr(inputs, 'validate'), 'loaded inputs need revalidation before compilation')
        inputs.validate()
        inputs.manifest['tensors']['fake'] = {'offset': 0}
        with self.assertRaisesRegex(ValueError, 'materialized compiler inputs changed'):
            inputs.validate()

    def test_resealed_payload_must_still_match_authenticated_package_hashes(self):
        from compiler.frontend.package_inputs import load_compile_inputs
        inputs = load_compile_inputs(self.snapshot, self.package, self.contract)
        for field in ('weights', 'scales'):
            with self.subTest(field=field):
                changed = replace(inputs, **{field: b'changed payload'})
                changed = replace(changed, materialized_sha256=changed._materialized_identity())
                with self.assertRaisesRegex(ValueError, 'package payload identity mismatch'):
                    changed.validate()
