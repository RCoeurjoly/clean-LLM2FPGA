"""Share pure cells only between exclusive register-bounded computations."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest


SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%value: i8, %clk: i1 {clk}, %reset: i1 {reset},
                        %go: i1 {go}) -> (%result: i8, %second: i8, %done: i1 {done}) {
    %true = hw.constant true
    %three = hw.constant 3 : i8
    %seven = hw.constant 7 : i8
    %a.left, %a.right, %a.out = calyx.std_add @a : i8, i8, i8
    %b.left, %b.right, %b.out = calyx.std_add @b : i8, i8, i8
    %source.in, %source.write_en, %source.clk, %source.reset, %source.out, %source.done =
      calyx.register @source : i8, i1, i1, i1, i8, i1
    %first.in, %first.write_en, %first.clk, %first.reset, %first.out, %first.done =
      calyx.register @first : i8, i1, i1, i1, i8, i1
    %last.in, %last.write_en, %last.clk, %last.reset, %last.out, %last.done =
      calyx.register @last : i8, i1, i1, i1, i8, i1
    calyx.wires {
      calyx.assign %result = %first.out : i8
      calyx.assign %second = %last.out : i8
      calyx.group @load {
        calyx.assign %source.in = %value : i8
        calyx.assign %source.write_en = %true : i1
        calyx.group_done %source.done : i1
      }
      calyx.group @one {
        calyx.assign %a.left = %source.out : i8
        calyx.assign %a.right = %three : i8
        calyx.assign %first.in = %a.out : i8
        calyx.assign %first.write_en = %true : i1
        calyx.group_done %first.done : i1
      }
      calyx.group @two {
        calyx.assign %b.left = %source.out : i8
        calyx.assign %b.right = %seven : i8
        calyx.assign %last.in = %b.out : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control { calyx.seq {
      calyx.enable @load calyx.enable @one calyx.enable @two
    } }
  } {toplevel}
}
'''


class RegisteredCombTests(unittest.TestCase):
    def transform(self, source=SOURCE, extra=()):
        result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--llm2fpga-share-register-bounded-comb', *extra],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_exclusive_register_bounded_adds_share_and_are_idempotent(self):
        result = self.transform()
        self.assertEqual(result.count('calyx.std_add'), 1)
        self.assertEqual(self.transform(result), result)

    def test_parallel_groups_remain_separate(self):
        source = SOURCE.replace('calyx.enable @one calyx.enable @two',
            'calyx.par { calyx.enable @one calyx.enable @two }')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_capture_with_separate_initialization_group_can_share(self):
        source = SOURCE.replace('        calyx.group_done %source.done : i1',
            '        calyx.assign %first.in = %three : i8\n'
            '        calyx.assign %first.write_en = %true : i1\n'
            '        calyx.group_done %source.done : i1')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 1)

    def test_invalid_continuous_capture_write_is_rejected(self):
        source = SOURCE.replace('        calyx.assign %first.write_en = %true : i1', '')
        source = source.replace('    calyx.wires {',
            '    calyx.wires {\n      calyx.assign %first.write_en = %true : i1')
        with self.assertRaisesRegex(AssertionError, 'failed to drive all necessary ports'):
            self.transform(source)

    def test_input_without_register_barrier_remains_separate(self):
        source = SOURCE.replace('%b.left = %source.out', '%b.left = %value')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_dependency_chain_remains_separate(self):
        source = SOURCE.replace('%b.left = %source.out', '%b.left = %a.out')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_observed_cell_remains_separate(self):
        for marker in ('external', 'protected', 'ref', 'llm2fpga.runtime_port'):
            with self.subTest(marker=marker):
                source = SOURCE.replace('calyx.std_add @b :',
                    'calyx.std_add @b {'+marker+' = false} :')
                self.assertEqual(self.transform(source).count('calyx.std_add'), 2)
        for symbol in ('b', 'last'):
            source = SOURCE.replace('%true = hw.constant true',
                '%true = hw.constant true {audit = {cell = [@'+symbol+']}}')
            self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_guarded_input_remains_separate(self):
        source = SOURCE.replace('%b.right = %seven', '%b.right = %go ? %seven')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_module_observers_preserve_cells_and_capture_registers(self):
        for observer in ('llm2fpga.audit = @main::@b',
                         'llm2fpga.audit = {cells = [@main::@b]}',
                         'llm2fpga.audit = @main::@last'):
            with self.subTest(observer=observer):
                source=SOURCE.replace('calyx.entrypoint = "main"',
                    'calyx.entrypoint = "main", '+observer)
                self.assertEqual(self.transform(source).count('calyx.std_add'),2)

    def test_module_opaque_observer_conservatively_disables_sharing(self):
        source=SOURCE.replace('calyx.entrypoint = "main"',
            'calyx.entrypoint = "main", llm2fpga.audit = #unknown<"observer">')
        self.assertEqual(self.transform(source,extra=('--allow-unregistered-dialect',)).count('calyx.std_add'),2)

    def test_capture_clock_override_remains_separate(self):
        source = SOURCE.replace('    calyx.wires {',
            '    calyx.wires {\n      calyx.assign %last.clk = %clk : i1')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_native_rtl_trace_and_cpu_match_repeated_transactions(self):
        from compiler.pipeline import CALYX_RESOURCE_PASSES
        calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        traces = []
        # A capture register initialized in another group models loop-carried
        # storage; its separate writer must remain unchanged by sharing.
        fixture = SOURCE.replace('        calyx.group_done %source.done : i1',
            '        calyx.assign %first.in = %three : i8\n'
            '        calyx.assign %first.write_en = %true : i1\n'
            '        calyx.group_done %source.done : i1')
        for source in (fixture, self.transform(fixture)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                emitted = subprocess.run([os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL'],
                    '--export-calyx'], input=source, text=True, capture_output=True, timeout=30)
                self.assertEqual(emitted.returncode, 0, emitted.stderr)
                (root/'source.futil').write_text(emitted.stdout)
                result = subprocess.run([str(calyx), 'source.futil', '-b', 'verilog',
                    '--synthesis', '--nested', *CALYX_RESOURCE_PASSES, '-l',
                    str(calyx.parent.parent/'share/calyx'), '-o', 'kernel.sv'],
                    cwd=root, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                (root/'tb.sv').write_text('''module tb;
reg clk=0,reset=1,go=0;reg[7:0]value=0;
wire[7:0]result,second;wire done;integer index,cycles,tick=0;
main dut(.*);always #5 clk=~clk;
always @(negedge clk)begin
 $display("TRACE %0d %b %b %b",tick,result,second,done);tick=tick+1;
end
initial begin
 repeat(2)@(negedge clk);reset=0;
 for(index=0;index<256;index=index+1)begin
  go=0;value=8'bx;repeat(2)@(negedge clk);
  value=index;go=1;cycles=0;
  while(!done&&cycles<30)begin @(negedge clk);cycles=cycles+1;end
  if(!done||result!==((index+3)&255)||second!==((index+7)&255))
   $fatal(1,"arithmetic/transaction mismatch");
  $display("RESULT %0d %0d %0d",index,result,second);
  go=0;@(negedge clk);
 end
 $finish;
end endmodule''')
                result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s',
                    'tb', '-o', 'sim', 'tb.sv', 'kernel.sv'], cwd=root,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([shutil.which('vvp'), 'sim'], cwd=root,
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                traces.append(result.stdout)
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(sum(line.startswith('RESULT ') for line in traces[1].splitlines()), 256)


if __name__ == '__main__':
    unittest.main()
