"""A typed reference primitive must remain a reference in native Futil."""
import os
from pathlib import Path
import subprocess
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


    def test_non_boolean_reference_flag_is_rejected(self):
        result = self.export('2 : i64')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('llm2fpga.ref must be boolean', result.stderr)
