"""Identical native compilation inputs must produce identical RTL bytes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class NativeEmissionReproducibilityTests(unittest.TestCase):
    def test_inferred_fsm_assignments_have_repeatable_serialization(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        library = Path(os.environ.get('LLM2FPGA_CALYX_LIBRARY',
                                      tool.parent.parent / 'share/calyx'))
        support = Path(os.environ.get('LLM2FPGA_TEST_SUPPORT',
                                      Path(__file__).resolve().parents[1]))
        source = support / 'compiler/tests/calyx/signed-divider-sharing.futil'
        for synthesis in (False, True):
            with self.subTest(synthesis=synthesis), tempfile.TemporaryDirectory() as work:
                root = Path(work)
                arguments = [str(tool), str(source), '-b', 'verilog',
                             '-x', 'cell-share:bounds=1,-1,-1',
                             '-x', 'tdcc:infer-fsms=true', '-l', str(library),
                             '-o', 'kernel.sv']
                if synthesis:
                    arguments.append('--synthesis')
                emissions = []
                for _ in range(6):
                    subprocess.run(arguments, cwd=root, check=True,
                                   capture_output=True, timeout=60)
                    emissions.append((root / 'kernel.sv').read_bytes())
                self.assertTrue(all(output == emissions[0] for output in emissions[1:]),
                                'Identical inferred-FSM inputs emit different RTL bytes')


if __name__ == '__main__':
    unittest.main()
