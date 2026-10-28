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
    def test_many_request_groups_have_bounded_lookup_work(self):
        import tempfile
        source = probe(data='calyx.assign %m.write_data = %zero : i64')
        start = source.index('      calyx.group @read {')
        end = source.index('\n      }', start) + len('\n      }')
        group = source[start:end]
        count = 32000
        source = source[:start] + '\n'.join(
            group.replace('@read', f'@read{i}') for i in range(count)) + source[end:]
        source = source.replace('calyx.enable @read }', 'calyx.seq { ' +
            ' '.join(f'calyx.enable @read{i}' for i in range(count)) + ' } }')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'requests.mlir'
            path.write_text(source)
            result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'], str(path),
                '--verify-each', '--llm2fpga-retire-memory-requests', '--emit-bytecode',
                '-o', os.devnull], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def run_pass(self, source):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native driver required')
        return subprocess.run([tool, '--verify-each', '--llm2fpga-retire-memory-requests'],
                              input=source, text=True, capture_output=True, timeout=30)

    def test_request_enable_waits_for_completion_retirement(self):
        result = self.run_pass(probe(data='calyx.assign %m.write_data = %zero : i64'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('calyx.enable @read_retire', result.stdout)
        self.assertIn('calyx.enable @read', result.stdout)
        self.assertIn('comb.xor', result.stdout)
        self.assertEqual(result.stdout.count('calyx.assign %m.content_en'), 1)
        again = self.run_pass(result.stdout)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, result.stdout)

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

    def test_retirement_guard_exports_through_native_serializer(self):
        result = self.run_pass(probe(data='calyx.assign %m.write_data = %zero : i64'))
        self.assertEqual(result.returncode, 0, result.stderr)
        translator = os.environ.get('LLM2FPGA_CALYX_TRANSLATE_TOOL')
        self.assertTrue(translator and Path(translator).is_file(), 'native serializer required')
        exported = subprocess.run([translator, '--export-calyx'], input=result.stdout,
                                  text=True, capture_output=True, timeout=30)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertIn('read_retire', exported.stdout)

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
