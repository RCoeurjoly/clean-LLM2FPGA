"""Many components must not retain one whole-file parser token tree."""
import os
from pathlib import Path
import resource
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

    def test_metadata_and_sourceinfo_survive_member_boundaries(self):
        for tail in ('metadata #{ opaque component { } }#',
                     'sourceinfo #{ FILES 0: /tmp/model.py POSITIONS 0: 0 7 }#',
                     'metadata #{ opaque }# sourceinfo #{ FILES 0: /tmp/model.py POSITIONS 0: 0 7 }#'):
            with self.subTest(tail=tail):
                result = self.parse(self.members() + tail)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('component worker', result.stdout)
                if 'metadata' in tail:
                    self.assertIn('opaque', result.stdout)
                if 'sourceinfo' in tail:
                    self.assertIn('/tmp/model.py', result.stdout)
                    self.assertIn('\n0: 0 7\n', result.stdout)

    def test_late_import_and_members_after_metadata_are_rejected(self):
        for tail in ('import "primitives/core.futil";',
                     'metadata #{ opaque }# component late() -> () { cells {} wires {} }'):
            result = self.parse(self.members() + tail)
            self.assertNotEqual(result.returncode, 0)

    def test_syntax_error_in_later_member_has_absolute_line(self):
        result = self.parse(self.members() + '\ncomponent broken() -> () { cells { ? } wires {} }\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('\n7 |component broken()', result.stderr)

    def test_many_components_parse_with_bounded_memory(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'components.futil'
            with source.open('w') as output:
                output.write('import "primitives/core.futil";\n')
                for component in range(64):
                    name = 'main' if component == 0 else f'worker{component}'
                    output.write(f'component {name}() -> () {{ cells {{ r=std_reg(1); }} wires {{\n')
                    for group in range(4096):
                        output.write(f'group g{group} {{ r.in=1\'d1; r.write_en=1\'d1; g{group}[done]=r.done; }}\n')
                    output.write('} control { g0; } }\n')
            def memory_limit():
                limit = 768 * 1024**2
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            # Parser/IR infrastructure only: discard output and skip optimization.
            result = subprocess.run([str(tool), str(source), '-p', 'none',
                '-b', 'calyx', '-o', os.devnull, '-l',
                str(tool.parent.parent / 'share/calyx')], preexec_fn=memory_limit,
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
