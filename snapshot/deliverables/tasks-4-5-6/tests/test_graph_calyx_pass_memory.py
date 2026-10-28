"""Native pass boundaries expose allocator lifetimes without changing emitted IR."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class PassMemoryTests(unittest.TestCase):
    def test_cleanup_boundary_measures_heap_without_changing_ir(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(32); }
 wires { group dead { r.in = 32'd1; r.write_en = 1'd1;
                       dead[done] = r.done; } }
 control { }
}
''')
            argv = [str(tool), str(source), '-p', 'dead-group-removal', '-b',
                    'calyx', '-l', str(tool.parent.parent / 'share/calyx')]
            quiet = subprocess.run(argv + ['--log', 'off'], capture_output=True,
                                   text=True, timeout=30, check=True)
            measured = subprocess.run(argv + ['--log', 'info'], capture_output=True,
                                      text=True, timeout=30, check=True)
            self.assertEqual(quiet.stdout, measured.stdout)
            self.assertNotIn('group dead', measured.stdout)
            self.assertNotIn('memory:pass:', quiet.stderr)
            for phase in ('start', 'end'):
                self.assertRegex(measured.stderr,
                    r'memory:pass:dead-group-removal:' + phase +
                    r' Some\(HeapSnapshot \{.*live_heap_bytes: [0-9]+')
