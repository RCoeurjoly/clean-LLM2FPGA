"""Memory contracts follow typed reference connectivity, not model cell names."""
import hashlib
import tempfile
from pathlib import Path
import unittest


def inventory(write=False):
    return {'schema_version': 1, 'memories': [
        dict(component='top', cell='arbitrary_storage', width=8, depth=3,
             address_width=2, physical=True, writes_disabled=True),
        dict(component='worker', cell='arbitrary_ref', width=8, depth=3,
             address_width=2, physical=False, writes_disabled=not write)],
        'references': [dict(caller='top', callee='worker', source='arbitrary_storage',
                            destination='arbitrary_ref')]}


class MemoryPlanTests(unittest.TestCase):
    def test_lut_threshold_uses_certified_group_width_and_preserves_latency(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image=Path(temporary)/'weights.hex'; image.write_text('01\nff\n07\n')
            images={('top','arbitrary_storage'):image}
            for limit,kind in ((0,'bram'),(11,'bram'),(12,'lutram')):
                bindings=plan_memories(inventory(),images,narrow_immutable=True,
                    lut_memory_max_bits=limit)['bindings']
                self.assertTrue(all(b['implementation']==kind and b['read_latency']==2
                    and b['width']==8 and b['storage_width']==4 and b['sign_extend'] for b in bindings))
            for limit,kind in ((12,'bram'),(24,'lutram')):
                bindings=plan_memories(inventory(),{},lut_memory_max_bits=limit)['bindings']
                self.assertTrue(all(b['implementation']==kind and b['writable']
                    and b['read_latency']==2 and 'storage_width' not in b for b in bindings))

    def test_lut_threshold_shares_contract_across_distinct_physical_callers(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            first,second=(Path(temporary)/name for name in ('first.hex','second.hex'))
            first.write_text('ff\n00\n01\n');second.write_text('10\n1f\n00\n')
            source=inventory();source['memories'].append(dict(source['memories'][0],cell='second'))
            source['references'].append(dict(source['references'][0],source='second'))
            images={('top','arbitrary_storage'):first,('top','second'):second}
            for limit,kind in ((12,'bram'),(18,'lutram')):
                bindings=plan_memories(source,images,narrow_immutable=True,
                    lut_memory_max_bits=limit)['bindings']
                self.assertTrue(all(b['storage_width']==6 and b['sign_extend']
                    and b['implementation']==kind for b in bindings))
                self.assertEqual({b['init_file'] for b in bindings},{'first.hex','second.hex',None})

    def test_lut_rom_fullwidth_contract_and_offchip_exclusion(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image=Path(temporary)/'weights.hex';image.write_text('80\n7f\n00\n')
            images={('top','arbitrary_storage'):image}
            for policy in ({'implementation':'lutram'}, {'lut_memory_max_bits':24}):
                bindings=plan_memories(inventory(),images,**policy)['bindings']
                self.assertTrue(all(b['implementation']=='lutram' and b['storage_width']==8
                    and b['sign_extend'] is False and not b['writable'] for b in bindings))
            bindings=plan_memories(inventory(),images,offchip_immutable=True,
                lut_memory_max_bits=24)['bindings']
            self.assertTrue(all(b['implementation']=='ddr3' and b['read_latency']==0
                and 'storage_width' not in b for b in bindings))

    def test_lut_threshold_requires_nonnegative_integer_and_bram_base(self):
        from compiler.memory_plan import plan_memories
        for value in (-1,True,1.0,None,'12'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'LUT memory'):
                plan_memories(inventory(),{},lut_memory_max_bits=value)
        for kind in ('register','lutram'):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'BRAM'):
                plan_memories(inventory(),{},implementation=kind,lut_memory_max_bits=1)

    def test_offchip_policy_keeps_scratch_onchip_and_preserves_reference_contracts(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'weights.hex'
            image.write_text('01\nff\n07\n')
            source = inventory()
            source['memories'].append(dict(source['memories'][0], cell='scratch', writes_disabled=False))
            result = plan_memories(source, {('top', 'arbitrary_storage'): image},
                                   offchip_immutable=True)
            for binding in result['bindings']:
                if binding['cell'] == 'scratch':
                    self.assertEqual((binding['implementation'], binding['read_latency'], binding['writable']), ('bram', 2, True))
                else:
                    self.assertEqual((binding['implementation'], binding['read_latency'], binding['writable']), ('ddr3', 0, False))
                    self.assertNotIn('storage_width', binding)
            runtime = plan_memories(inventory(), {}, offchip_immutable=True)
            self.assertTrue(all(b['implementation'] == 'bram' for b in runtime['bindings']))

    def test_offchip_policy_rejects_unqualified_narrowing(self):
        from compiler.memory_plan import plan_memories
        with self.assertRaisesRegex(ValueError, 'off-chip.*narrow'):
            plan_memories(inventory(), {}, offchip_immutable=True, narrow_immutable=True)

    def test_narrowing_preserves_signed_and_unsigned_image_words(self):
        from compiler.memory_plan import plan_memories
        for words, width, signed in (('01\nff\n07\n', 4, True),
                                     ('00\n08\n0f\n', 4, False),
                                     ('00\n00\n00\n', 1, False)):
            with self.subTest(words=words), tempfile.TemporaryDirectory() as temporary:
                image = Path(temporary) / 'weights.hex'
                image.write_text(words)
                bindings = plan_memories(inventory(),
                    {('top', 'arbitrary_storage'): image}, narrow_immutable=True)['bindings']
                self.assertTrue(all(b['storage_width'] == width and
                                    b['sign_extend'] == signed and b['width'] == 8
                                    for b in bindings))

    def test_shared_reference_uses_one_lossless_width_for_distinct_images(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            first, second = (Path(temporary) / name for name in ('first.hex', 'second.hex'))
            first.write_text('ff\n00\n01\n')
            second.write_text('10\n1f\n00\n')
            source = inventory()
            source['memories'].append(dict(source['memories'][0], cell='second'))
            source['references'].append(dict(source['references'][0], source='second'))
            bindings = plan_memories(source, {('top', 'arbitrary_storage'): first,
                ('top', 'second'): second}, narrow_immutable=True)['bindings']
            self.assertTrue(all(b['storage_width'] == 6 and b['sign_extend'] for b in bindings))
            self.assertEqual({b['init_file'] for b in bindings}, {'first.hex', 'second.hex', None})

    def test_full_width_and_runtime_memories_keep_legacy_storage(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'full.hex'
            image.write_text('80\n7f\n00\n')
            for images in ({}, {('top', 'arbitrary_storage'): image}):
                bindings = plan_memories(inventory(), images, narrow_immutable=True)['bindings']
                self.assertTrue(all('storage_width' not in b and 'sign_extend' not in b
                                    for b in bindings))

    def test_geometry_and_readonly_contract_follow_reference_edges(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'weights.hex'
            image.write_text('01\nff\n07\n')
            result = plan_memories(inventory(), {('top', 'arbitrary_storage'): image})
            self.assertEqual(len(result['bindings']), 2)
            self.assertTrue(all(not b['writable'] for b in result['bindings']))
            self.assertTrue(all(b['depth'] == 3 and b['read_latency'] == 2
                                for b in result['bindings']))
            self.assertEqual(result['bindings'][0]['init_file'], 'weights.hex')
            self.assertIsNone(result['bindings'][1]['init_file'])

    def test_enabled_callee_write_rejects_immutable_image(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'weights.hex'
            image.write_text('01\nff\n07\n')
            with self.assertRaisesRegex(ValueError, 'immutable.*write'):
                plan_memories(inventory(write=True), {('top', 'arbitrary_storage'): image})

    def test_uninitialized_runtime_memory_is_mutable_across_reference(self):
        from compiler.memory_plan import plan_memories
        result = plan_memories(inventory(), {})
        self.assertTrue(all(binding['writable'] for binding in result['bindings']))

    def test_initializer_mapping_uses_graph_origin_not_cell_spelling(self):
        from compiler.memory_plan import initializers_from_origins
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'weights.hex'
            image.write_text('01\nff\n07\n')
            source = inventory()
            source['memories'][1]['origin'] = dict(kind='BUFFER', target='weights',
                sha256=hashlib.sha256(bytes([1, 255, 7])).hexdigest())
            self.assertEqual(initializers_from_origins(source, {'weights': image}),
                             {('top', 'arbitrary_storage'): image})
            image.write_text('02\nff\n07\n')
            with self.assertRaisesRegex(ValueError, 'tensor identity'):
                initializers_from_origins(source, {'weights': image})
            with self.assertRaisesRegex(ValueError, 'missing frozen'):
                initializers_from_origins(source, {})

    def test_mixed_frozen_and_uninitialized_callers_are_rejected(self):
        from compiler.memory_plan import plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'weights.hex'
            image.write_text('01\nff\n07\n')
            source = inventory()
            other = dict(source['memories'][0], cell='runtime_storage')
            source['memories'].append(other)
            source['references'].append(dict(source['references'][0], source='runtime_storage'))
            with self.assertRaisesRegex(ValueError, 'mixed initializer'):
                plan_memories(source, {('top', 'arbitrary_storage'): image})

    def test_distinct_frozen_callers_keep_their_own_images(self):
        from compiler.memory_plan import initializers_from_origins, plan_memories
        with tempfile.TemporaryDirectory() as temporary:
            images = {}
            source = inventory()
            for target, values in (('first', bytes([1, 255, 7])), ('second', bytes([2, 3, 4]))):
                image = Path(temporary) / (target + '.hex')
                image.write_text(''.join(f'{value:02x}\n' for value in values))
                images[target] = image
                origin = dict(kind='BUFFER', target=target,
                              sha256=hashlib.sha256(values).hexdigest())
                if target == 'first':
                    source['memories'][0]['origin'] = origin
                else:
                    source['memories'].append(dict(source['memories'][0],
                                                  cell='second_storage', origin=origin))
                    source['references'].append(dict(source['references'][0], source='second_storage'))
            bindings = initializers_from_origins(source, images)
            self.assertEqual(bindings, {('top', 'arbitrary_storage'): images['first'],
                                        ('top', 'second_storage'): images['second']})
            plan = plan_memories(source, bindings)
            self.assertTrue(all(not binding['writable'] for binding in plan['bindings']))
            self.assertEqual({binding['init_file'] for binding in plan['bindings']},
                             {'first.hex', 'second.hex', None})

    def test_conflicting_origins_on_one_physical_memory_are_rejected(self):
        from compiler.memory_plan import initializers_from_origins
        with tempfile.TemporaryDirectory() as temporary:
            images = {}
            source = inventory()
            for index, (target, values) in enumerate((('first', bytes([1, 255, 7])),
                                                      ('second', bytes([2, 3, 4])))):
                image = Path(temporary) / (target + '.hex')
                image.write_text(''.join(f'{value:02x}\n' for value in values))
                images[target] = image
                source['memories'][index]['origin'] = dict(kind='BUFFER', target=target,
                    sha256=hashlib.sha256(values).hexdigest())
            with self.assertRaisesRegex(ValueError, 'ambiguous frozen'):
                initializers_from_origins(source, images)

    def test_missing_reference_or_geometry_mismatch_fails(self):
        from compiler.memory_plan import plan_memories
        source = inventory()
        source['references'][0]['source'] = 'absent'
        with self.assertRaisesRegex(ValueError, 'absent'):
            plan_memories(source, {})
        source = inventory()
        source['memories'][1]['depth'] = 4
        with self.assertRaisesRegex(ValueError, 'geometry'):
            plan_memories(source, {})


if __name__ == '__main__':
    unittest.main()
