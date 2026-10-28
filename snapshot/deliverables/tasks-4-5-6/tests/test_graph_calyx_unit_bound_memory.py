"""Bound-one combinational cells cannot merge and need no conflict graph."""
import os
import json
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest


class UnitBoundMemoryTests(unittest.TestCase):
    def test_frequency_diagnostics_and_legacy_override_are_preserved(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { a=std_add(32); b=std_add(32); }
 wires {
  group first { a.left=32'd1; a.right=32'd2; first[done]=1'd1; }
  group second { b.left=32'd3; b.right=32'd4; second[done]=1'd1; }
 }
 control { seq { first; second; } }
}
''')
            for legacy, expected_map in ((False, []), (True, [
                    {'original': 'b', 'new': 'a', 'cell_type': 'std_add_WIDTH_32'}])):
                sharing_map = Path(directory) / 'sharing-map.json'
                argv = [str(tool), str(source), '-p', 'cell-share',
                        '-x', 'cell-share:bounds=1,-1,-1', '-x',
                        f'cell-share:emit-share-map={sharing_map}', '-b', 'calyx', '-l',
                        str(tool.parent.parent / 'share/calyx')]
                if legacy:
                    argv.extend(('-x', 'cell-share:calyx-2020=true'))
                result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(sharing_map.read_text()), {'main': expected_map})
            for legacy, expected in ((False, {'1': 2}), (True, {'2': 1})):
                with self.subTest(legacy=legacy):
                    frequencies = Path(directory) / 'frequencies.json'
                    argv = [str(tool), str(source), '-p', 'cell-share',
                        '-x', 'cell-share:bounds=1,-1,-1', '-x',
                        f'cell-share:print-share-freqs={frequencies}',
                        '-b', 'calyx', '-o', os.devnull, '-l',
                        str(tool.parent.parent / 'share/calyx')]
                    if legacy:
                        argv.extend(('-x', 'cell-share:calyx-2020=true'))
                    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(frequencies.read_text()),
                                     {'main': {'std_add_WIDTH_32': expected}})

    def test_nonsharing_combinational_class_under_memory_limit(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            with source.open('w') as output:
                output.write('import "primitives/core.futil";\n'
                             'component main() -> () { cells {\n')
                for index in range(32000):
                    output.write(f'a{index}=std_add(32);\n')
                output.write('} wires { group work {\n')
                for index in range(32000):
                    output.write(f"a{index}.left=32'd1; a{index}.right=32'd2;\n")
                output.write("work[done]=1'd1; } } control { work; } }\n")

            def cap():
                limit = 256 * 1024**2
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            result = subprocess.run([str(tool), str(source), '-p', 'cell-share',
                '-x', 'cell-share:bounds=1,-1,-1', '--log', 'info', '-b', 'calyx',
                '-o', os.devnull, '-l', str(tool.parent.parent / 'share/calyx')],
                preexec_fn=cap, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pass:cell-share:end', result.stderr)
