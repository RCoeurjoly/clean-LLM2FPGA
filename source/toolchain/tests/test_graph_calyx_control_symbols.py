"""Control symbol validation keeps all checks while sharing scoped lookups."""
import os
from pathlib import Path
import subprocess
import unittest


def source(control, groups=None):
    if groups is None:
        control = "calyx.enable @work\ncalyx.if %cond.in with @condition { calyx.enable @work }\n" + control
        groups = """calyx.group @work {
          calyx.assign %finish.in = %true : i1
          calyx.group_done %finish.out : i1
        }
        calyx.comb_group @condition { calyx.assign %cond.in = %true : i1 }"""
    return """module attributes {calyx.entrypoint = "main"} {
      calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
        %true = hw.constant true
        %cond.in, %cond.out = calyx.std_wire @cond : i1, i1
        %finish.in, %finish.out = calyx.std_wire @finish : i1, i1
        calyx.wires {""" + groups + """}
        calyx.control { calyx.seq {""" + control + """} }
      } {toplevel}
    }"""


class CalyxControlSymbolTests(unittest.TestCase):
    def verify(self, document, timeout=30):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx tool required')
        return subprocess.run([tool, '--verify-each'], input=document,
                              capture_output=True, text=True, timeout=timeout)

    def test_valid_control_and_optional_condition_group(self):
        control = """calyx.enable @work
        calyx.if %cond.in with @condition { calyx.enable @work }
        calyx.while %cond.in with @condition { calyx.enable @work }
        calyx.if %cond.out { calyx.enable @work }
        calyx.while %cond.out { calyx.enable @work }"""
        result = self.verify(source(control))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_group_is_rejected_for_each_control_kind(self):
        for control in ('calyx.enable @missing',
                        'calyx.if %cond.in with @missing {}',
                        'calyx.while %cond.in with @missing {}'):
            with self.subTest(control=control):
                result = self.verify(source(control))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('does not exist', result.stderr)

    def test_wrong_group_kind_is_rejected(self):
        for control, diagnostic in (
                ('calyx.enable @condition', 'which is a combinational group'),
                ('calyx.if %cond.in with @work {}', 'not a combinational group'),
                ('calyx.while %cond.in with @work {}', 'not a combinational group')):
            with self.subTest(control=control):
                result = self.verify(source(control))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)

    def test_undriven_condition_is_rejected(self):
        for kind in ('if', 'while'):
            with self.subTest(kind=kind):
                result = self.verify(source('calyx.' + kind + ' %finish.in with @condition {}'))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('no driver was found', result.stderr)

    def test_unused_group_is_still_rejected(self):
        groups = ''.join('calyx.group @' + name + ' { calyx.assign %finish.in = %true : i1\n'
                         'calyx.group_done %finish.out : i1 }\n' for name in ('work', 'unused'))
        result = self.verify(source('calyx.enable @work', groups))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is unused in the control execution schedule', result.stderr)

    def test_many_groups_verify_with_bounded_lookup_work(self):
        # A compiler scalability gate, independent of SCF lowering or scheduling.
        # Each enabled group has a nonconstant completion port and one assignment.
        count = 30000
        groups = ''.join('calyx.group @g' + str(i) + ' { calyx.assign %finish.in = %true : i1\n'
                         'calyx.group_done %finish.out : i1 }\n' for i in range(count))
        control = ''.join('calyx.enable @g' + str(i) + '\n' for i in range(count))
        result = self.verify(source(control, groups), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
