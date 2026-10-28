"""Lift only private pure-cell drives whose observations stay in one group."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%value: i8, %clk: i1 {clk}, %reset: i1 {reset},
                        %go: i1 {go}) -> (%result: i8, %done: i1 {done}) {
    %true = hw.constant true
    %three = hw.constant 3 : i8
    %adder.left, %adder.right, %adder.out = calyx.std_add @adder : i8, i8, i8
    %last.in, %last.write_en, %last.clk, %last.reset, %last.out, %last.done =
      calyx.register @last : i8, i1, i1, i1, i8, i1
    calyx.wires {
      calyx.assign %result = %last.out : i8
      calyx.group @compute {
        calyx.assign %adder.left = %value : i8
        calyx.assign %adder.right = %three : i8
        calyx.assign %last.in = %adder.out : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control { calyx.enable @compute }
  } {toplevel}
}
'''


class CombInputLiftingTests(unittest.TestCase):
    def transform(self, source=SOURCE):
        tool = os.environ.get('LLM2FPGA_CALYX_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(), 'native Calyx driver required')
        result = subprocess.run([tool, '--verify-each',
            '--llm2fpga-lift-exclusive-comb-inputs'], input=source, text=True,
            capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_pure_inputs_move_outside_group_but_stateful_write_stays(self):
        result = self.transform()
        group = result.index('calyx.group @compute')
        self.assertLess(result.index('calyx.assign %adder.left'), group)
        self.assertLess(result.index('calyx.assign %adder.right'), group)
        self.assertGreater(result.index('calyx.assign %last.in'), group)
        self.assertEqual(self.transform(result), result)

    def test_public_output_escape_keeps_operand_gating(self):
        source = SOURCE.replace('%result = %last.out', '%result = %adder.out')
        result = self.transform(source)
        self.assertGreater(result.index('calyx.assign %adder.left'),
                           result.index('calyx.group @compute'))

    def test_external_or_protected_cell_keeps_operand_gating(self):
        for marker in ('external', 'protected'):
            with self.subTest(marker=marker):
                source = SOURCE.replace('calyx.std_add @adder :',
                    'calyx.std_add @adder {'+marker+' = true} :')
                result = self.transform(source)
                self.assertGreater(result.index('calyx.assign %adder.left'),
                                   result.index('calyx.group @compute'))

    def test_symbolic_observer_keeps_operand_gating(self):
        # Symbol-use collection must cross the component symbol table and
        # recursively visit aggregate attributes on otherwise unrelated ops.
        for attribute in ('audit_cell = @adder',
                          'audit = {observed = [@adder]}'):
            with self.subTest(attribute=attribute):
                source = SOURCE.replace('%true = hw.constant true',
                    '%true = hw.constant true {'+attribute+'}')
                result = self.transform(source)
                self.assertGreater(result.index('calyx.assign %adder.left'),
                                   result.index('calyx.group @compute'))

    def test_shared_input_drives_in_two_groups_are_not_lifted(self):
        source = SOURCE.replace('    }\n    calyx.control', '''
      calyx.group @other {
        calyx.assign %adder.left = %three : i8
        calyx.assign %adder.right = %value : i8
        calyx.assign %last.in = %adder.out : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control''').replace('calyx.enable @compute',
                              'calyx.seq { calyx.enable @compute calyx.enable @other }')
        result = self.transform(source)
        self.assertGreater(result.index('calyx.assign %adder.left'),
                           result.index('calyx.group @compute'))

    def test_guarded_input_is_not_lifted(self):
        source = SOURCE.replace('%adder.left = %value', '%adder.left = %go ? %value')
        result = self.transform(source)
        self.assertGreater(result.index('calyx.assign %adder.left'),
                           result.index('calyx.group @compute'))

    def test_group_local_value_is_not_moved_out_of_scope(self):
        source = SOURCE.replace('        calyx.assign %adder.left',
            '        %local = hw.constant 7 : i8\n        calyx.assign %adder.left')
        source = source.replace('%adder.right = %three', '%adder.right = %local')
        result = self.transform(source)
        self.assertGreater(result.index('calyx.assign %adder.left'),
                           result.index('calyx.group @compute'))

    def test_rtl_public_trace_preserves_all_bytes_and_idle_input_changes(self):
        from compiler.pipeline import CALYX_RESOURCE_PASSES
        translator = os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL']
        calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        iverilog, vvp = shutil.which('iverilog'), shutil.which('vvp')
        self.assertIsNotNone(iverilog, 'native Icarus required')
        self.assertIsNotNone(vvp, 'native Icarus runtime required')
        traces = []
        for source in (SOURCE, self.transform()):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                exported = subprocess.run([translator, '--export-calyx'], input=source,
                    text=True, capture_output=True, timeout=30)
                self.assertEqual(exported.returncode, 0, exported.stderr)
                (root/'source.futil').write_text(exported.stdout)
                subprocess.run([str(calyx), 'source.futil', '-b', 'verilog',
                    '--synthesis', '--nested', *CALYX_RESOURCE_PASSES,
                    '-l', str(calyx.parent.parent/'share/calyx'), '-o', 'kernel.sv'],
                    cwd=root, check=True, capture_output=True, text=True, timeout=60)
                (root/'tb.sv').write_text('''module tb;
reg clk=0, reset=1, go=0;
reg [7:0] value=0;
wire [7:0] result;
wire done;
main dut(.*);
always #5 clk=~clk;
integer index, cycles, tick=0;
always @(negedge clk) begin
  $display("TRACE %0d %0d %0d", tick,result,done); tick=tick+1;
end
initial begin
  repeat(2) @(negedge clk); reset=0;
  for(index=0; index<256; index=index+1) begin
    go=0; value=255-index; repeat(2) @(negedge clk);
    value=index; go=1; cycles=0;
    while(!done && cycles<20) begin @(negedge clk); cycles=cycles+1; end
    if(!done) $fatal(1, "fixture did not complete");
    if(result !== ((index+3) & 255)) $fatal(1, "wrong arithmetic result");
    $display("RESULT %0d %0d %0d",index,result,cycles);
    go=0; @(negedge clk);
  end
  $finish;
end
endmodule
''')
                subprocess.run([iverilog, '-g2012', '-s', 'tb', '-o', 'simulator',
                    'tb.sv', 'kernel.sv'], cwd=root, check=True, capture_output=True,
                    text=True, timeout=30)
                result = subprocess.run([vvp, 'simulator'], cwd=root, check=True,
                    capture_output=True, text=True, timeout=30)
                traces.append(result.stdout)
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(sum(line.startswith('RESULT ') for line in traces[1].splitlines()), 256)


if __name__ == '__main__':
    unittest.main()
