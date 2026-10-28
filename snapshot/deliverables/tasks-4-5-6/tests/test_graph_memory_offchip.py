"""Off-chip binding is explicitly dynamic and must externalize physical storage."""
import unittest
import test_graph_memory_binding as fixtures


class OffchipMemoryBindingTests(unittest.TestCase):
    def validate(self, **kwargs):
        return fixtures.MemoryBindingTests(methodName='runTest').validate(**kwargs)

    def test_readonly_offchip_binding_exports_dynamic_external_interface(self):
        result = self.validate(changes={'implementation': 'ddr3', 'read_latency': 0},
                               bind=True, export_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('closure_ddr3_memory', result.stdout)
        self.assertIn('@external', result.stdout)
        self.assertNotIn('@interval', result.stdout)

    def test_offchip_binding_rejects_fixed_bram_latency(self):
        result = self.validate(changes={'implementation': 'ddr3', 'read_latency': 2})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('implementation/latency mismatch', result.stderr)

    def test_offchip_binding_rejects_writable_storage_until_ordering_is_qualified(self):
        result = self.validate(changes={'implementation': 'ddr3', 'read_latency': 0,
                                       'writable': True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('off-chip binding requires immutable full-width storage', result.stderr)

    def test_offchip_binding_rejects_narrow_storage_until_packing_is_qualified(self):
        result = self.validate(changes={'implementation': 'ddr3', 'read_latency': 0,
                                       'storage_width': 4, 'sign_extend': False},
                               image=b'01\n03\n07\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('off-chip binding requires immutable full-width storage', result.stderr)
