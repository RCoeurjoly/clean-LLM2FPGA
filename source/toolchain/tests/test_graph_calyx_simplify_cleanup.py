"""Native group retirement must preserve groups consumed by invokes."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


class SimplifyCleanupTests(unittest.TestCase):
    def test_completed_component_retires_original_assignment_storage(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        count = 2000
        cells = '\n'.join(f'r{i} = std_reg(1);' for i in range(count))
        assignments = '\n'.join(f'r{i}.in = 1\'d0;' for i in range(count))
        source_text = ('import "primitives/core.futil";\n'
            'component main() -> () { cells {' + cells + '} wires {'
            'comb group condition {' + assignments + '}}'
            'control { if r0.out with condition {} else {} }}')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(source_text)
            result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                '-p', 'simplify-with-control', '-x',
                'simplify-with-control:retire-dead-groups=true', '--log', 'info',
                '-l', str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        sizes = {}
        for phase in ('finish', 'after-cleanup'):
            marker = f'memory:simplify-with-control:main:{phase} '
            line = next((line for line in result.stderr.splitlines()
                         if marker in line), None)
            self.assertIsNotNone(line, result.stderr)
            sizes[phase] = int(re.search(r'live_heap_bytes: (\d+)', line)[1])
        self.assertLess(sizes['after-cleanup'], sizes['finish'])

    def test_cleanup_matches_native_composition_and_preserves_invokes(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        source_text = '''import "primitives/core.futil";
import "primitives/binary_operators.futil";
component main() -> () {
 cells { r = std_reg(1); m = std_mult_pipe(32); a = std_add(32); }
 wires {
  comb group condition { r.in = 1'd0; }
  comb group arguments { a.left = 32'd1; a.right = 32'd2; }
 }
 control { seq {
  if r.out with condition { } else { }
  invoke m(left=a.out, right=a.out)() with arguments;
 } }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text(source_text)
            def run(extra):
                result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                    '-p', 'simplify-with-control', '--log', 'info', '-l',
                    str(tool.parent.parent / 'share/calyx'), *extra],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result
            original = run([])
            canonical = run(['-p', 'dead-group-removal'])
            candidate = run(['-x', 'simplify-with-control:retire-dead-groups=true'])
        self.assertIn('simplify-with-control:main:cleanup:end', candidate.stderr)
        self.assertEqual(candidate.stdout, canonical.stdout)
        self.assertIn('comb group arguments', candidate.stdout)
        self.assertIn('with arguments', candidate.stdout)
        self.assertNotIn('comb group condition', candidate.stdout)
        self.assertIn('comb group condition', original.stdout)
        self.assertNotIn('simplify-with-control:main:cleanup:end', original.stderr)
