"""Invocation diagnostics separate allocator observation from IR lowering."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class InvokeTimingTests(unittest.TestCase):
    def test_per_invoke_timing_separates_snapshots_and_preserves_ir(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        fixture = '''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(1); }
 wires {}
 control { seq { invoke r(in=1'd1, write_en=1'd1)();
                 invoke r(in=1'd0, write_en=1'd1)(); } }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(fixture)
            results = []
            for level in ('info', 'warn', 'debug'):
                result = subprocess.run([
                    str(tool), str(source), '-p', 'compile-invoke',
                    '-b', 'calyx', '--log', level, '-l',
                    str(tool.parent.parent / 'share/calyx')],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                results.append(result)
        self.assertEqual(results[0].stdout, results[1].stdout)
        self.assertEqual(results[0].stdout, results[2].stdout)
        timings = re.findall(
            r'compile-invoke:timing:main snapshot_start_ns=(\d+) '
            r'lowering_ns=(\d+) snapshot_end_ns=(\d+)', results[0].stderr)
        self.assertEqual(len(timings), 2, results[0].stderr)
        self.assertTrue(all(int(value) >= 0 for row in timings for value in row))
        self.assertTrue(all(int(row[0]) == 0 and int(row[2]) == 0
                            for row in timings), results[0].stderr)
        self.assertNotIn('memory:compile-invoke:main:invoke:', results[0].stderr)
        self.assertIn('memory:compile-invoke:main:invoke:start', results[2].stderr)
        self.assertIn('memory:compile-invoke:main:invoke:end', results[2].stderr)
        self.assertNotIn('compile-invoke:timing:', results[1].stderr)
