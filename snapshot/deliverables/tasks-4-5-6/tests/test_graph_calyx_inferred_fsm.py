"""Inferred dynamic FSM output decoding must not infer storage."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class InferredFsmTests(unittest.TestCase):
    def test_dynamic_state_outputs_are_combinational(self):
        calyx, yosys = os.environ.get('CALYX'), os.environ.get('YOSYS')
        if not calyx or not yosys:
            self.skipTest('native CALYX and YOSYS are required')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = ['import "primitives/core.futil";',
                      'import "primitives/binary_operators.futil";',
                      'component main(advance: 1) -> (out: 8) {',
                      'cells { r = std_reg(8); a = std_add(8); }',
                      'wires { out = r.out;']
            for i in range(16):
                source.append(f"group g{i} {{ a.left = r.out; a.right = 8'd{i+1}; "
                              f"r.in = a.out; r.write_en = 1'd1; g{i}[done] = advance; }}")
            source += ['}', 'control { seq {'] + [f'g{i};' for i in range(16)] + ['} }', '}']
            (root / 'fixture.futil').write_text('\n'.join(source) + '\n')
            subprocess.run([calyx, str(root / 'fixture.futil'), '-p', 'external',
                '-l', str(Path(calyx).parent.parent / 'share/calyx'),
                '-b', 'verilog', '--synthesis', '--disable-verify',
                '-x', 'tdcc:infer-fsms=true', '-o', str(root / 'fixture.sv')],
                check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([yosys, '-p',
                f'read_verilog -sv {root}/fixture.sv; hierarchy -top main; '
                f'proc; flatten; opt; check -assert; write_json {root}/netlist.json'],
                check=True, capture_output=True, text=True, timeout=30)
            cells = json.loads((root / 'netlist.json').read_text())['modules']['main']['cells']
            latches = [name for name, cell in cells.items() if 'latch' in cell['type'].lower()]
            self.assertEqual(latches, [], 'dynamic FSM state outputs inferred latches')


if __name__ == '__main__':
    unittest.main()
