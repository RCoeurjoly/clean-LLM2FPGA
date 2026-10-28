"""Native process pruning must avoid alias analysis on process-free modules."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class NativeProcPruneTests(unittest.TestCase):
    def run_design(self, source):
        yosys = os.environ.get('YOSYS') or shutil.which('yosys')
        if not yosys:
            raise RuntimeError('Native Yosys required')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'design.v').write_text(source)
            result = subprocess.run(
                [yosys, '-Q', '-T', '-p',
                 f'read_verilog {root}/design.v; hierarchy -top main; '
                 f'proc_prune; proc; opt; write_json {root}/out.json'],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            module = json.loads((root / 'out.json').read_text())['modules']['main']
            return module, result.stdout

    def test_empty_process_module_skips_alias_analysis(self):
        module, log = self.run_design(
            'module main(input [31:0] a,output [31:0] y);assign y=a;endmodule'
        )
        self.assertEqual(module['ports']['a']['bits'], module['ports']['y']['bits'])
        self.assertIn('Skipping alias analysis for main: no processes', log)

    def test_overwritten_assignment_pruned(self):
        module, log = self.run_design(
            'module main(input [31:0] a,b,output reg [31:0] y);'
            'always @* begin y=a;y=b;end endmodule'
        )
        self.assertEqual(module['ports']['b']['bits'], module['ports']['y']['bits'])
        self.assertNotIn(
            'Skipping alias analysis for main: no processes',
            log.split('Executing PROC pass')[0],
        )

    def test_clocked_process_retains_register_and_input(self):
        module, _ = self.run_design(
            'module main(input clk,input [31:0] a,output reg [31:0] y);'
            'always @(posedge clk)y<=a;endmodule'
        )
        cells = list(module['cells'].values())
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]['type'], '$dff')
        self.assertEqual(cells[0]['connections']['D'], module['ports']['a']['bits'])
        self.assertEqual(cells[0]['connections']['Q'], module['ports']['y']['bits'])


if __name__ == '__main__':
    unittest.main()
