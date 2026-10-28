"""Native inout demotion and its no-inout connection-scan fast path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class NativeDeminoutTests(unittest.TestCase):
    def run_design(self, source, selection='select *'):
        yosys = os.environ.get('YOSYS') or shutil.which('yosys')
        if not yosys:
            raise RuntimeError('Native Yosys is required for these tests')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'design.v').write_text(source)
            commands = (
                f'read_verilog {root}/design.v; hierarchy -top main; '
                f'{selection}; deminout; select *; write_json {root}/out.json'
            )
            result = subprocess.run(
                [yosys, '-Q', '-T', '-p', commands],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            modules = json.loads((root / 'out.json').read_text())['modules']
            return modules, result.stdout

    def test_no_inout_skips_connection_analysis(self):
        modules, log = self.run_design(
            'module main(input [1023:0] a,output [1023:0] y);'
            'assign y=a; endmodule'
        )
        ports = modules['main']['ports']
        self.assertEqual(ports['a']['bits'], ports['y']['bits'])
        self.assertIn(
            'Skipping connection analysis for main: no selected inout ports', log
        )

    def test_input_demoted(self):
        modules, _ = self.run_design(
            'module main(inout a,output y); assign y=~a; endmodule'
        )
        self.assertEqual(modules['main']['ports']['a']['direction'], 'input')

    def test_output_demoted(self):
        modules, _ = self.run_design(
            'module main(input a,inout y); assign y=~a; endmodule'
        )
        self.assertEqual(modules['main']['ports']['y']['direction'], 'output')

    def test_true_tristate_retained(self):
        modules, _ = self.run_design(
            "module main(input a,input en,inout y,output z);"
            "assign y=en?a:1'bz; assign z=y; endmodule"
        )
        self.assertEqual(modules['main']['ports']['y']['direction'], 'inout')

    def test_partial_selection_demotes_only_selected_port(self):
        modules, _ = self.run_design(
            'module main(inout a,inout b,output y,output z);'
            'assign y=~a; assign z=~b; endmodule', 'select main/a'
        )
        ports = modules['main']['ports']
        self.assertEqual(ports['a']['direction'], 'input')
        self.assertEqual(ports['b']['direction'], 'inout')

    def test_unselected_inout_retained(self):
        modules, _ = self.run_design(
            'module main(inout a,output y);assign y=~a;endmodule', 'select main/y'
        )
        self.assertEqual(modules['main']['ports']['a']['direction'], 'inout')

    def test_child_module_inout_demoted(self):
        modules, _ = self.run_design(
            'module child(inout a,output y);assign y=~a;endmodule '
            'module main(input a,output y);child c(a,y);endmodule'
        )
        self.assertEqual(modules['child']['ports']['a']['direction'], 'input')
        self.assertEqual(modules['main']['ports']['a']['direction'], 'input')

    def test_aliased_ports_retained(self):
        modules, _ = self.run_design(
            'module main(inout a,output y);assign y=a;endmodule'
        )
        ports = modules['main']['ports']
        self.assertEqual(ports['a']['direction'], 'inout')
        self.assertEqual(ports['a']['bits'], ports['y']['bits'])


if __name__ == '__main__':
    unittest.main()
