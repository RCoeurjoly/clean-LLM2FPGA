"""Native diagnostic phase boundaries must locate full-model export failures."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


class CalyxPhaseLoggingTests(unittest.TestCase):
    def test_simplify_with_control_logs_component_allocation_boundaries(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(1); }
 wires { comb group condition { r.in = 1'd0; } }
 control { if r.out with condition { } else { } }
}
''')
            result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                '-p', 'simplify-with-control', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        positions = []
        for phase in ('start', 'control-ports:end', 'conversion:end', 'finish'):
            marker = f'memory:simplify-with-control:main:{phase} '
            line = next((line for line in result.stderr.splitlines()
                         if marker in line), None)
            self.assertIsNotNone(line, result.stderr)
            self.assertIn('live_heap_bytes:', line)
            positions.append(result.stderr.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_allocator_snapshot_accounts_for_live_and_free_heap(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('component main() -> () { cells {} wires {} control {} }')
            result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                '-p', 'none', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        for phase in ('workspace:end', 'ir:end'):
            line = next((line for line in result.stderr.splitlines()
                         if f'memory:{phase} ' in line), None)
            self.assertIsNotNone(line, result.stderr)
            values = {key: int(value) for key, value in re.findall(
                r'(arena_bytes|live_heap_bytes|free_heap_bytes|mapped_bytes): (\d+)', line)}
            self.assertEqual(set(values), {'arena_bytes', 'live_heap_bytes',
                                          'free_heap_bytes', 'mapped_bytes'})
            self.assertEqual(values['arena_bytes'], values['live_heap_bytes'] + values['free_heap_bytes'])

    def test_native_log_orders_parser_ir_pass_and_backend_boundaries(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(1); }
 wires { group write { r.in = 1'd1; r.write_en = 1'd1; write[done] = r.done; } }
 control { write; }
}
''')
            result = subprocess.run([str(tool), str(source), '-b', 'verilog',
                '--log', 'info', '-l', str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        positions = []
        for phase in ('workspace', 'ir', 'passes', 'backend'):
            for boundary in ('start', 'end'):
                marker = f'phase:{phase}:{boundary}'
                self.assertIn(marker, result.stderr)
                positions.append(result.stderr.index(marker))
        self.assertEqual(positions, sorted(positions))
        self.assertIn('module main', result.stdout)

    def test_cell_share_log_identifies_internal_allocation_stages(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('component main() -> () { cells {} wires {} control {} }')
            result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                '-p', 'cell-share', '--log', 'info', '-l',
                str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        markers = []
        for stage in ('liveness', 'graphs', 'control-data', 'conflicts', 'coloring'):
            markers.extend(f'cell-share:main:{stage}:{edge}' for edge in ('start', 'end'))
        for marker in markers:
            self.assertIn(marker, result.stderr)
        positions = [result.stderr.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def test_native_log_brackets_each_selected_pass(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('component main() -> () { cells {} wires {} control {} }')
            result = subprocess.run([str(tool), str(source), '-b', 'calyx',
                '-p', 'dead-cell-removal', '-p', 'cell-share', '--log', 'info',
                '-l', str(tool.parent.parent / 'share/calyx')],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        markers = ['phase:passes:start']
        for name in ('dead-cell-removal', 'cell-share'):
            markers.extend((f'pass:{name}:start', f'pass:{name}:end'))
        markers.append('phase:passes:end')
        for marker in markers:
            self.assertIn(marker, result.stderr)
        positions = [result.stderr.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
