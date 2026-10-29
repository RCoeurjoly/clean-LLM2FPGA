"""Calyx request groups must not consume a previous memory completion."""
import os
from pathlib import Path
import subprocess
import unittest
try:
    from tests.test_graph_memory_contracts import probe
except ModuleNotFoundError:
    from test_graph_memory_contracts import probe

class MemoryRetirementTests(unittest.TestCase):

    def run_pass(self, source):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native driver required')
        return subprocess.run([tool, '--verify-each', '--llm2fpga-retire-memory-requests'],
                              input=source, text=True, capture_output=True, timeout=30)


    def test_existing_retirement_name_collision_is_rejected(self):
        source = probe(data='calyx.assign %m.write_data = %zero : i64')
        source = source.replace('calyx.group @read {',
            'calyx.group @read_retire { calyx.group_done %m.done : i1 }\n'
            '      calyx.group @read {')
        source = source.replace('calyx.enable @read }',
            'calyx.seq { calyx.enable @read_retire calyx.enable @read } }')
        result = self.run_pass(source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('memory retirement group name collision', result.stderr)


    def test_already_bound_memory_rejects_wrong_pass_order(self):
        try:
            from tests.test_graph_memory_binding import MemoryBindingTests
        except ModuleNotFoundError:
            from test_graph_memory_binding import MemoryBindingTests
        bound = MemoryBindingTests().validate(bind=True)
        self.assertEqual(bound.returncode, 0, bound.stderr)
        result = self.run_pass(bound.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('retirement must precede memory binding', result.stderr)
