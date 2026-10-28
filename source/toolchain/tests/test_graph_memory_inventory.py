"""Inventory is native typed IR serialization, including invoke references."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_graph_memory_binding import MULTI_SOURCE


class MemoryInventoryTests(unittest.TestCase):
    def test_typed_geometry_and_reference_edges_are_exported(self):
        tool = os.environ['LLM2FPGA_CALYX_PASS_TOOL']
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / 'inventory.json'
            result = subprocess.run([tool, '--verify-each',
                '--llm2fpga-export-memory-inventory=inventory=' + str(inventory)],
                input=MULTI_SOURCE.replace('calyx.seq_mem @weights <[3] x 8> [2] :',
                    'calyx.seq_mem @weights <[3] x 8> [2] {llm2fpga.input_origin = '
                    '{index = 0 : i64, name = "b_weights", kind = "BUFFER", target = "weights"}, '
                    'llm2fpga.runtime_port = "prompt"} :'),
                text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(inventory.read_text())
            self.assertEqual(document['schema_version'], 1)
            cells = {(item['component'], item['cell']): item for item in document['memories']}
            self.assertEqual(set(cells), {('main', 'm'), ('reader', 'weights')})
            self.assertEqual(cells['main', 'm']['depth'], 3)
            self.assertEqual(cells['main', 'm']['width'], 8)
            self.assertEqual(cells['main', 'm']['address_width'], 2)
            self.assertTrue(cells['main', 'm']['physical'])
            self.assertFalse(cells['reader', 'weights']['physical'])
            self.assertTrue(cells['reader', 'weights']['writes_disabled'])
            self.assertEqual(cells['reader', 'weights']['origin']['target'], 'weights')
            self.assertEqual(cells['reader', 'weights']['origin']['index'], 0)
            self.assertEqual(cells['reader', 'weights']['runtime_port'], 'prompt')
            self.assertNotIn('runtime_port', cells['main', 'm'])
            self.assertEqual(document['references'], [dict(caller='main', callee='reader',
                source='m', destination='weights')])


if __name__ == '__main__':
    unittest.main()
