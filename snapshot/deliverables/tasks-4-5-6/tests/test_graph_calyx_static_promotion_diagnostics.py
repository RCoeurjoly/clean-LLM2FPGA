"""Native diagnostics identify component-level static promotion boundaries."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class StaticPromotionDiagnosticsTests(unittest.TestCase):
    def test_phase_markers_preserve_emitted_ir(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { r = std_reg(32); }
 wires { group write { r.in = 32'd7; r.write_en = 1'd1;
                      write[done] = r.done; } }
 control { seq { write; write; } }
}
''')
            outputs = []
            for level in ('off', 'info'):
                result = subprocess.run([str(tool), str(source), '-p', 'static-inference',
                    '-p', 'static-promotion', '-b', 'calyx', '--log', level,
                    '-l', str(tool.parent.parent / 'share/calyx')],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(result.stdout)
                if level == 'info':
                    previous = -1
                    for phase in ('start', 'timing:end', 'continuous-access:end', 'finish'):
                        marker = 'memory:static-promotion:main:' + phase
                        position = result.stderr.find(marker)
                        self.assertGreater(position, previous, marker + '\n' + result.stderr)
                        previous = position
            self.assertEqual(outputs[0], outputs[1])
