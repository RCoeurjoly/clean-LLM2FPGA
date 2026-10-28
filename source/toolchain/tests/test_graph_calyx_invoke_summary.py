"""Repeated reference arguments must not repeatedly scan a completed callee."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InvokeSummaryTests(unittest.TestCase):
    def test_repeated_invokes_share_one_completed_callee_summary(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        fixture = '''import "primitives/core.futil";
component child() -> () {
 cells { ref read = std_reg(1); ref write = std_reg(1); }
 wires { group copy { write.in = read.out; write.write_en = 1'd1;
                     copy[done] = write.done; } }
 control { copy; }
}
component main() -> () {
 cells { c = child(); a = std_reg(1); b = std_reg(1); }
 wires {}
 control { seq {
  invoke c[read=a, write=b]()();
  invoke c[read=b, write=a]()();
  invoke c[read=a, write=b]()();
 } }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(fixture)
            result = subprocess.run([str(tool), str(source), '-p', 'compile-invoke',
                '-b', 'calyx', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count('compile-invoke:ref-port-summary:child '), 1)
        self.assertNotIn('invoke c[', result.stdout)
        self.assertIn('c.read_out = a.out;', result.stdout)
        self.assertIn('c.read_out = b.out;', result.stdout)
        self.assertIn('b.in = c.write_in;', result.stdout)
        self.assertIn('a.in = c.write_in;', result.stdout)
        self.assertNotIn('c.read_in', result.stdout)

    def test_static_assignment_summary_and_distinct_callee_uses(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        fixture = '''import "primitives/core.futil";
static<1> component writer() -> () {
 cells { ref r = std_reg(1); }
 wires { static<1> group update { r.in = 1'd1; r.write_en = 1'd1; } }
 control { static seq { update; } }
}
component reader() -> () {
 cells { ref r = std_reg(1); local = std_reg(1); }
 wires { group read { local.in = r.out; local.write_en = 1'd1;
                     read[done] = local.done; } }
 control { read; }
}
component main() -> () {
 cells { c = writer(); d = reader(); a = std_reg(1); b = std_reg(1); }
 wires {}
 control { seq { static invoke c[r=a]()(); invoke d[r=b]()(); } }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(fixture)
            result = subprocess.run([str(tool), str(source), '-p', 'compile-invoke',
                '-b', 'calyx', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('writer', 'reader'):
            self.assertEqual(result.stderr.count(f'compile-invoke:ref-port-summary:{name} '), 1)
        self.assertIn('a.in = c.r_in;', result.stdout)
        self.assertIn('d.r_out = b.out;', result.stdout)
        self.assertNotIn('c.r_out = a.out;', result.stdout)
        self.assertNotIn('b.in = d.r_in;', result.stdout)
