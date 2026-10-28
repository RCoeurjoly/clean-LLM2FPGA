"""A typed reference primitive must remain a reference in native Futil."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


def source(flag):
    return '''module attributes {calyx.entrypoint = "main"} {
  hw.module.extern @memory(in %addr0: i2, in %clk: i1 {calyx.clk}, in %reset: i1 {calyx.reset}, in %content_en: i1 {calyx.go}, in %write_en: i1, in %write_data: i8, out read_data: i8, out done: i1 {calyx.done}) attributes {filename = "memory.sv"}
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.primitive @m of @memory : i2, i1, i1, i1, i1, i8, i8, i1
    %worker.clk, %worker.reset, %worker.go, %worker.done = calyx.instance @worker of @reader : i1, i1, i1, i1
    calyx.wires {}
    calyx.control { calyx.invoke @worker[weights = m]() -> () }
  } {toplevel}
  calyx.component @reader(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %weights.addr0, %weights.clk, %weights.reset, %weights.content_en, %weights.write_en, %weights.write_data, %weights.read_data, %weights.done = calyx.primitive @weights of @memory {llm2fpga.ref = ''' + flag + '''} : i2, i1, i1, i1, i1, i8, i8, i1
    %true = hw.constant true
    %bridge.in, %bridge.out = calyx.std_wire @bridge : i1, i1
    calyx.wires {
      calyx.assign %bridge.in = %true : i1
      calyx.assign %done = %bridge.out : i1
    }
    calyx.control {}
  }
}
'''


class CalyxExportTests(unittest.TestCase):
    def export(self, flag):
        tool = os.environ.get('LLM2FPGA_CALYX_TRANSLATE_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native translator required')
        return subprocess.run([tool, '--export-calyx'], input=source(flag),
                              text=True, capture_output=True, timeout=30)

    def test_reference_keyword_survives_export(self):
        result = self.export('true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ref weights = memory();', result.stdout)
        self.assertIn('m = memory();', result.stdout)
        self.assertNotIn('ref m =', result.stdout)
        self.assertIn('weights = m', result.stdout)

    def test_false_reference_flag_preserves_physical_cell(self):
        result = self.export('false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('weights = memory();', result.stdout)
        self.assertNotIn('ref weights =', result.stdout)

    def test_non_boolean_reference_flag_is_rejected(self):
        result = self.export('2 : i64')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('llm2fpga.ref must be boolean', result.stderr)

    def test_reference_program_passes_native_calyx_validation(self):
        tool = os.environ.get('LLM2FPGA_CALYX_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'pinned Calyx compiler required')
        result = self.export('true')
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / 'reference.futil'
            program.write_text(result.stdout)
            # This gate checks frontend reference typing; RTL replay follows binding.
            (root / 'memory.sv').write_text('''module memory(
input [1:0] addr0, input clk, reset, content_en, write_en,
input [7:0] write_data, output [7:0] read_data, output done);
endmodule
''')
            library = Path(tool).resolve().parents[1] / 'share/calyx'
            checked = subprocess.run([tool, str(program), '-b', 'none', '-l', str(library)],
                                     text=True, capture_output=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
