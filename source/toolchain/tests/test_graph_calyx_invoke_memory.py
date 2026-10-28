"""Locate allocation growth inside native invocation lowering."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

class InvokeMemoryTests(unittest.TestCase):
    def test_component_and_invocation_allocation_boundaries(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component child(x: 1) -> (y: 1) {
 cells { r = std_reg(1); }
 wires { y = r.out; group write { r.in = x; r.write_en = 1'd1; write[done] = r.done; } }
 control { write; }
}
component main() -> () {
 cells { c = child(); r = std_reg(1); }
 wires { comb group args { r.in = 1'd1; } }
 control { invoke c(x=r.out)() with args; }
}
''')
            result = subprocess.run([str(tool), str(source), '-p', 'compile-invoke',
                '-b', 'calyx', '--log', 'debug', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        markers = [f'memory:compile-invoke:main:{phase} ' for phase in
                   ('start', 'invoke:start', 'invoke:end', 'finish')]
        for marker in markers:
            line = next((line for line in result.stderr.splitlines() if marker in line), None)
            self.assertIsNotNone(line, result.stderr)
            self.assertIn('live_heap_bytes:', line)
        self.assertEqual([result.stderr.index(m) for m in markers],
                         sorted(result.stderr.index(m) for m in markers))
        self.assertNotIn('invoke c(', result.stdout)
