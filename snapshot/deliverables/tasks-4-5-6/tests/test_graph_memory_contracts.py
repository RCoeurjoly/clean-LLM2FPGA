"""Native Calyx passes must prove omitted write data is inert."""
import os
from pathlib import Path
import subprocess
import unittest


def probe(enable='false', data='', enable_source='%enable'):
    return '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
    %zero = hw.constant 0 : i64
    %enable = hw.constant ''' + enable + '''
    %addr = hw.constant 0 : i4
    %true = hw.constant true
    %m.addr0, %m.clk, %m.reset, %m.content_en, %m.write_en, %m.write_data, %m.read_data, %m.done = calyx.seq_mem @m <[16] x 64> [4] : i4, i1, i1, i1, i1, i64, i64, i1
    calyx.wires {
      calyx.group @read {
        calyx.assign %m.addr0 = %addr : i4
        calyx.assign %m.content_en = %true : i1
        calyx.assign %m.write_en = ''' + enable_source + ''' : i1
        ''' + data + '''
        calyx.group_done %m.done : i1
      }
    }
    calyx.control { calyx.enable @read }
  } {toplevel}
}
'''


class MemoryContractTests(unittest.TestCase):
    def run_pass(self, source, arguments=()):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx pass driver is required')
        return subprocess.run([tool, '--llm2fpga-complete-calyx-memory-contracts', *arguments],
                              input=source, text=True, capture_output=True, timeout=30)

    def test_zero_enable_completes_missing_write_data(self):
        result = self.run_pass(probe())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('calyx.assign %m.write_data'), 1)
        again = self.run_pass(result.stdout)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, result.stdout)

    def test_enabled_write_without_data_is_rejected(self):
        result = self.run_pass(probe('true'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing write_data', result.stderr)

    def test_existing_write_data_is_preserved(self):
        result = self.run_pass(probe('true', 'calyx.assign %m.write_data = %zero : i64'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('calyx.assign %m.write_data'), 1)

    def test_dynamic_enable_without_data_is_rejected(self):
        result = self.run_pass(probe(enable_source='%go'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing write_data', result.stderr)

    def test_guarded_zero_is_rejected_without_proving_guard_coverage(self):
        result = self.run_pass(probe(enable_source='%true ? %enable'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing write_data', result.stderr)

    def test_multiple_enable_drivers_are_rejected(self):
        result = self.run_pass(probe(data='calyx.assign %m.write_en = %enable : i1'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing write_data', result.stderr)

    def test_unknown_pass_registration_is_failure(self):
        result = self.run_pass(probe(), ('--llm2fpga-unregistered-pass',))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown command line argument', result.stderr)
