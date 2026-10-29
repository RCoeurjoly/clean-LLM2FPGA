"""Many components must not retain one whole-file parser token tree."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class CalyxParserMemoryTests(unittest.TestCase):
    def parse(self, text):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(text)
            return subprocess.run([str(tool), str(source), '-p', 'none', '-b', 'calyx',
                '-l', str(tool.parent.parent / 'share/calyx')], capture_output=True,
                text=True, timeout=30)

    def members(self):
        return '''import "primitives/core.futil";
// braces } { in comments do not delimit members
component main() -> () { cells {} wires {} control {} }
/* next component */
component worker() -> () { cells {} wires {} control {} }
'''


    def test_late_import_and_members_after_metadata_are_rejected(self):
        for tail in ('import "primitives/core.futil";',
                     'metadata #{ opaque }# component late() -> () { cells {} wires {} }'):
            result = self.parse(self.members() + tail)
            self.assertNotEqual(result.returncode, 0)

    def test_syntax_error_in_later_member_has_absolute_line(self):
        result = self.parse(self.members() + '\ncomponent broken() -> () { cells { ? } wires {} }\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('\n7 |component broken()', result.stderr)
