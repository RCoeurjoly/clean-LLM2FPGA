"""Range certificates must derive from every authenticated tensor word."""
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class FrozenRangeTests(unittest.TestCase):
    def build(self, manifest, images, output):
        module = importlib.import_module('compiler.frozen_ranges')
        return module.build_frozen_range_certificate(manifest, images, output)

    def inputs(self, root, values):
        payload = b''.join((value % (1 << 64)).to_bytes(8, 'little') for value in values)
        record = {'index': 0, 'name': 'parameter', 'kind': 'PARAMETER',
                  'target': 'arbitrary.tensor', 'dtype': 'torch.int64',
                  'shape': [len(values)], 'sha256': hashlib.sha256(payload).hexdigest()}
        manifest = root / 'graph-inputs.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'inputs': [record]}))
        image = root / 'input.hex'
        image.write_text(''.join(f'{value % (1 << 64):016x}\n' for value in values))
        return manifest, {'arbitrary.tensor': image}

    def test_signed_bounds_include_unsampled_last_word(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, images = self.inputs(root, [-128, 127, -(1 << 63)])
            result = self.build(manifest, images, root / 'ranges.json')
            entry = result['tensors'][0]
            self.assertEqual(entry['value_width'], 64)
            self.assertEqual(entry['minimum'], -(1 << 63))
            self.assertFalse(result['readonly_proven'])

    def test_signed_and_unsigned_extension_are_distinct(self):
        for values, extension in (([-128, 127], 'signed'), ([0, 255], 'unsigned')):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest, images = self.inputs(root, values)
                result = self.build(manifest, images, root / 'ranges.json')
                entry = result['tensors'][0]
                self.assertEqual(entry['value_width'], 8)
                self.assertEqual(entry['storage_width'], 64)
                self.assertEqual(entry['extension'], extension)

    def test_changed_image_rejects_and_invalidates_old_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, images = self.inputs(root, [-128, 127])
            images['arbitrary.tensor'].write_text('0000000000000000\n0000000000000000\n')
            output = root / 'ranges.json'
            output.write_text('old acceptance')
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.build(manifest, images, output)
            self.assertFalse(output.exists())

    def test_output_cannot_delete_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, images = self.inputs(root, [-128, 127])
            image = images['arbitrary.tensor']
            before = image.read_bytes()
            with self.assertRaisesRegex(ValueError, 'overlap'):
                self.build(manifest, images, image)
            self.assertEqual(image.read_bytes(), before)

    def test_runtime_input_cannot_receive_a_frozen_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, images = self.inputs(root, [-128, 127])
            document = json.loads(manifest.read_text())
            document['inputs'][0]['kind'] = 'USER_INPUT'
            document['inputs'][0]['sha256'] = None
            manifest.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'runtime|image'):
                self.build(manifest, images, root / 'ranges.json')

    def test_output_cannot_delete_compiler_source(self):
        module = importlib.import_module('compiler.frozen_ranges')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, images = self.inputs(root, [-128, 127])
            compiler = root / 'compiler'
            compiler.mkdir()
            source = compiler / 'existing.py'
            source.write_text('preserve source')
            with patch.object(module, 'COMPILER_ROOT', compiler):
                with self.assertRaisesRegex(ValueError, 'overlap'):
                    self.build(manifest, images, source)
            self.assertEqual(source.read_text(), 'preserve source')
