"""Exact live-control collection must handle a dense register frontier."""
import os
from pathlib import Path
import resource
import re
import subprocess
import tempfile
import unittest


class LiveControlMemoryTests(unittest.TestCase):
    def test_dense_live_control_frontier_under_memory_limit(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        count = 4000
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'frontier.futil'
            with source.open('w') as output:
                output.write('import "primitives/core.futil";\n'
                             'component main() -> () { cells { sink=std_reg(32);\n')
                for index in range(count):
                    output.write(f'r{index}=std_reg(32);\n')
                output.write('} wires {\n')
                for index in range(count):
                    output.write(f"group init{index} {{ r{index}.in=32'd{index}; "
                                 f"r{index}.write_en=1'd1; init{index}[done]=r{index}.done; }}\n")
                    output.write(f"group drain{index} {{ sink.in=r{index}.out; "
                                 f"sink.write_en=1'd1; drain{index}[done]=sink.done; }}\n")
                output.write('} control { seq { ')
                for prefix in ('init', 'drain'):
                    for index in range(count):
                        output.write(f'{prefix}{index}; ')
                output.write('} } }\n')

            def memory_limit():
                limit = 512 * 1024**2
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            result = subprocess.run([str(tool), str(source), '-p', 'cell-share',
                '-x', 'cell-share:bounds=1,-1,-1',
                '-x', 'cell-share:retire-dead-cells=true', '-b', 'calyx', '-o', os.devnull,
                '--log', 'info', '-l', str(tool.parent.parent / 'share/calyx')],
                preexec_fn=memory_limit, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cell-share:main:control-data:end', result.stderr)
        self.assertIn('pass:cell-share:end', result.stderr)

        type_snapshots = {}
        for phase in ('before-type-analysis-retirement', 'after-type-analysis-retirement'):
            marker = f'memory:cell-share:main:{phase}:std_reg_WIDTH_32'
            line = next((line for line in result.stderr.splitlines() if marker in line), '')
            self.assertTrue(line, f'missing {marker}')
            values = dict((name, int(value)) for name, value in re.findall(
                r'(live_heap_bytes|mapped_bytes): ([0-9]+)', line))
            self.assertEqual(set(values), {'live_heap_bytes', 'mapped_bytes'})
            type_snapshots[phase] = sum(values.values())
        self.assertLess(type_snapshots['after-type-analysis-retirement'],
                        type_snapshots['before-type-analysis-retirement'])
        self.assertLess(result.stderr.index('after-type-analysis-retirement'),
                        result.stderr.index('cell-share:main:conflicts:end'))

        snapshots = {}
        for phase in ('before-live-analysis-retirement', 'after-live-analysis-retirement',
                      'after-live-analysis-reclamation',
                      'before-coloring', 'after-analysis-retirement'):
            marker = f'memory:cell-share:main:{phase}'
            line = next((line for line in result.stderr.splitlines() if marker in line), '')
            self.assertTrue(line, f'missing {marker}')
            values = dict((name, int(value)) for name, value in re.findall(
                r'(live_heap_bytes|mapped_bytes): ([0-9]+)', line))
            self.assertEqual(set(values), {'live_heap_bytes', 'mapped_bytes'})
            snapshots[phase] = sum(values.values())
        self.assertLess(snapshots['after-analysis-retirement'], snapshots['before-coloring'])

        self.assertLess(snapshots['after-live-analysis-retirement'],
                        snapshots['before-live-analysis-retirement'])
        self.assertLess(result.stderr.index('after-live-analysis-retirement'),
                        result.stderr.index('cell-share:main:conflicts:start'))

        self.assertEqual(snapshots['after-live-analysis-reclamation'],
                         snapshots['after-live-analysis-retirement'])
        self.assertLess(result.stderr.index('after-live-analysis-reclamation'),
                        result.stderr.index('cell-share:main:conflicts:start'))
        self.assertIn('cell-share:main:early-allocator-reclamation Some(', result.stderr)
