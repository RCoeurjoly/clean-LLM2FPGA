"""Read-value bounds follow immutable reference groups without packing storage."""
import importlib
from pathlib import Path
import tempfile
import unittest
from compiler.memory_plan import plan_memories


def inventory(write=False):
    return {'schema_version': 1, 'memories': [
        {'component': 'top', 'cell': 'source', 'width': 64, 'depth': 2,
         'address_width': 1, 'physical': True, 'writes_disabled': True},
        {'component': 'child', 'cell': 'reference', 'width': 64, 'depth': 2,
         'address_width': 1, 'physical': False, 'writes_disabled': not write}],
        'references': [{'caller': 'top', 'source': 'source', 'callee': 'child', 'destination': 'reference'}]}


class ReadValueBoundsTests(unittest.TestCase):
    def propose(self, source, images):
        module = importlib.import_module('compiler.read_value_bounds')
        return module.plan_read_value_bounds(source, images, offchip_immutable=True)

    def test_read_bounds_preserve_ddr_storage_and_cover_references(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'data.hex'
            image.write_text('ffffffffffffff80\n000000000000007f\n')
            images = {('top', 'source'): image}
            baseline = plan_memories(inventory(), images, offchip_immutable=True)
            bounded = self.propose(inventory(), images)
            for before, after in zip(baseline['bindings'], bounded['bindings'], strict=True):
                self.assertEqual({k: after[k] for k in before}, before)
                self.assertEqual(after['value_width'], 8)
                self.assertTrue(after['value_sign_extend'])
                self.assertNotIn('storage_width', after)

    def test_enabled_reference_write_cannot_receive_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'data.hex'
            image.write_text('0000000000000001\n0000000000000002\n')
            with self.assertRaisesRegex(ValueError, 'write'):
                self.propose(inventory(True), {('top', 'source'): image})

    def test_reference_group_combines_distinct_image_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'first.hex', root / 'second.hex'
            first.write_text('ffffffffffffff80\n0000000000000001\n')
            second.write_text('000000000000ffff\n0000000000000000\n')
            source = inventory()
            source['memories'].append(dict(source['memories'][0], cell='second'))
            source['references'].append(dict(source['references'][0], source='second'))
            result = self.propose(source, {('top', 'source'): first, ('top', 'second'): second})
            self.assertTrue(all(b['value_width'] == 32 and b['value_sign_extend']
                                for b in result['bindings']))

    def test_runtime_memory_keeps_full_width_without_range_claim(self):
        result = self.propose(inventory(), {})
        self.assertTrue(all('value_width' not in b for b in result['bindings']))
