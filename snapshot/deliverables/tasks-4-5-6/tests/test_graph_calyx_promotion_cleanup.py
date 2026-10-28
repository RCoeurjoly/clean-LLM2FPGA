"""Retire unused promoted originals without removing live dynamic groups."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class PromotionCleanupTests(unittest.TestCase):
    def compile_fixture(self, control):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main(condition: 1) -> () {
 cells { r = std_reg(32); }
 wires { group write { r.in = 32'd7; r.write_en = 1'd1;
                      write[done] = r.done; } }
 control { ''' + control + ''' }
}
''')
            result = subprocess.run([str(tool), str(source), '-p', 'static-inference',
                '-p', 'static-promotion', '-x', 'static-promotion:retire-dead-groups=true',
                '-b', 'calyx', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')], capture_output=True,
                text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

    def test_unused_original_is_retired_at_component_finish(self):
        result = self.compile_fixture('seq { write; write; }')
        self.assertNotRegex(result.stdout, r'(?m)^\s+group write(?:<|\s)')
        self.assertRegex(result.stdout, r'static<1> group write\d+')
        self.assertIn('static-promotion:main:cleanup:end', result.stderr)

    def test_dynamically_used_original_survives_promotion_cleanup(self):
        result = self.compile_fixture('seq { while condition { write; } seq { write; write; } }')
        self.assertRegex(result.stdout, r'(?m)^\s+group write(?:<|\s)')
        self.assertRegex(result.stdout, r'static<1> group write\d+')
        self.assertIn('while condition', result.stdout)
        self.assertIn('static-promotion:main:cleanup:end', result.stderr)
