"""Dead invoke groups must be retired before static analysis duplicates IR."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InvokeCleanupTests(unittest.TestCase):
    def test_native_schedules_retire_dead_groups_before_static_inference(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(32); }
 wires { comb group args { r.in = 32'd7; } }
 control { seq { invoke r(in=32'd7)() with args;
                 invoke r(in=32'd9)() with args; } }
}
''')
            for schedule in ('pre-opt', 'fsm-opt'):
                with self.subTest(schedule=schedule):
                    result = subprocess.run([str(tool), str(source), '-p', schedule,
                        '-b', 'calyx', '--log', 'info', '-l',
                        str(tool.parent.parent / 'share/calyx')],
                        capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    marker = 'pass:compile-invoke:end'
                    following = result.stderr[result.stderr.index(marker):]
                    self.assertLess(following.index('pass:dead-group-removal:start'),
                                    following.index('pass:static-inference:start'))
