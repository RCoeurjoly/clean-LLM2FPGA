"""Conflict matrices must not overlap across independent resource types."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class TypeGraphLifetimeTests(unittest.TestCase):
    def test_completed_type_releases_graph_before_next_type(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        count = 2000
        parts = ['import "primitives/core.futil"; component main() -> () { cells {']
        for width in (16, 32, 64):
            parts.append(f'sink{width}=std_reg({width});')
            parts.extend(f'r{width}_{i}=std_reg({width});' for i in range(count))
        parts.append('} wires {')
        for width in (16, 32, 64):
            for i in range(count):
                parts.append(f"group init{width}_{i} {{ r{width}_{i}.in={width}'d{i}; "
                             f"r{width}_{i}.write_en=1'd1; init{width}_{i}[done]=r{width}_{i}.done; }}")
                parts.append(f"group drain{width}_{i} {{ sink{width}.in=r{width}_{i}.out; "
                             f"sink{width}.write_en=1'd1; drain{width}_{i}[done]=sink{width}.done; }}")
        parts.append('} control { seq {')
        for prefix in ('init', 'drain'):
            for width in (16, 32, 64):
                parts.extend(f'{prefix}{width}_{i};' for i in range(count))
        parts.append('} } }')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'mixed.futil'
            source.write_text('\n'.join(parts))
            result = subprocess.run([str(tool), str(source), '-p', 'cell-share',
                '-x', 'cell-share:bounds=1,-1,-1',
                '-x', 'cell-share:retire-dead-cells=true', '-b', 'calyx', '-o', os.devnull,
                '--log', 'info', '-l', str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        active = None
        completed = set()
        for line in result.stderr.splitlines():
            match = re.search(r'cell-share:main:type-graph:(start|released):([^ ]+)', line)
            if not match:
                continue
            phase, kind = match.groups()
            if phase == 'start':
                self.assertIsNone(active, 'previous conflict graph still active')
                active = kind
            else:
                self.assertEqual(active, kind)
                completed.add(kind)
                active = None
        self.assertIsNone(active)
        self.assertEqual(completed, {f'std_reg_WIDTH_{w}' for w in (16, 32, 64)},
                         'missing per-type graph retirement boundaries')
