"""Only identical, continuously driven built-in pure computations may merge."""
import os
import subprocess
import shutil
import tempfile
from pathlib import Path
import unittest

SOURCE = '''module attributes {calyx.entrypoint = "main"} {
  calyx.component @main(%value: i8, %clk: i1 {clk}, %reset: i1 {reset},
                        %go: i1 {go}) -> (%result: i8, %second: i8, %done: i1 {done}) {
    %true = hw.constant true
    %three = hw.constant 3 : i8
    %a.left, %a.right, %a.out = calyx.std_add @a : i8, i8, i8
    %b.left, %b.right, %b.out = calyx.std_add @b : i8, i8, i8
    %last.in, %last.write_en, %last.clk, %last.reset, %last.out, %last.done =
      calyx.register @last : i8, i1, i1, i1, i8, i1
    calyx.wires {
      calyx.assign %a.left = %value : i8
      calyx.assign %a.right = %three : i8
      calyx.assign %b.left = %value : i8
      calyx.assign %b.right = %three : i8
      calyx.assign %result = %a.out : i8
      calyx.assign %second = %b.out : i8
      calyx.group @empty {
        calyx.assign %last.in = %a.out : i8
        calyx.assign %last.write_en = %true : i1
        calyx.group_done %last.done : i1
      }
    }
    calyx.control { calyx.enable @empty }
  } {toplevel}
}
'''


class IdenticalCombTests(unittest.TestCase):
    def transform(self, source=SOURCE):
        result = subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--llm2fpga-deduplicate-identical-comb'],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_identical_continuous_computations_merge_and_are_idempotent(self):
        result = self.transform()
        self.assertEqual(result.count('calyx.std_add'), 1)
        self.assertEqual(self.transform(result), result)

    def test_different_operands_do_not_merge(self):
        result = self.transform(SOURCE.replace('%b.right = %three', '%b.right = %value'))
        self.assertEqual(result.count('calyx.std_add'), 2)

    def test_guarded_inputs_do_not_merge(self):
        result = self.transform(SOURCE.replace('%b.right = %three', '%b.right = %go ? %three'))
        self.assertEqual(result.count('calyx.std_add'), 2)

    def test_observation_markers_preserve_distinct_cells(self):
        for marker in ('external', 'protected', 'ref', 'llm2fpga.runtime_port'):
            with self.subTest(marker=marker):
                result = self.transform(SOURCE.replace('calyx.std_add @b :',
                    'calyx.std_add @b {' + marker + ' = false} :'))
                self.assertEqual(result.count('calyx.std_add'), 2)

    def test_symbolic_observer_preserves_named_cell(self):
        source = SOURCE.replace('%true = hw.constant true',
                                '%true = hw.constant true {audit_cell = @b}')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_dependency_chain_is_not_merged_with_its_producer(self):
        source = SOURCE.replace('%b.left = %value', '%b.left = %a.out')
        self.assertEqual(self.transform(source).count('calyx.std_add'), 2)

    def test_deduplication_reaches_fixed_point_with_reverse_declared_chain(self):
        source = SOURCE.replace('    %a.left',
            '    %c.left, %c.right, %c.out = calyx.std_add @c : i8, i8, i8\n'
            '    %d.left, %d.right, %d.out = calyx.std_add @d : i8, i8, i8\n    %a.left', 1)
        source = source.replace('      calyx.assign %result',
            '      calyx.assign %c.left = %a.out : i8\n'
            '      calyx.assign %c.right = %three : i8\n'
            '      calyx.assign %d.left = %b.out : i8\n'
            '      calyx.assign %d.right = %three : i8\n      calyx.assign %result', 1)
        source = source.replace('%result = %a.out', '%result = %c.out')
        source = source.replace('%second = %b.out', '%second = %d.out')
        result = self.transform(source)
        self.assertEqual(result.count('calyx.std_add'), 2)
        self.assertEqual(self.transform(result), result)

    def test_native_rtl_trace_preserves_all_bytes_and_unknown_idle_inputs(self):
        from compiler.pipeline import CALYX_RESOURCE_PASSES
        translator = os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL']
        calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        iverilog, vvp = shutil.which('iverilog'), shutil.which('vvp')
        self.assertIsNotNone(iverilog)
        self.assertIsNotNone(vvp)
        traces = []
        for source in (SOURCE, self.transform()):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                emitted = subprocess.run([translator, '--export-calyx'], input=source,
                    text=True, capture_output=True, timeout=30)
                self.assertEqual(emitted.returncode, 0, emitted.stderr)
                (root/'source.futil').write_text(emitted.stdout)
                subprocess.run([str(calyx), 'source.futil', '-b', 'verilog',
                    '--synthesis', '--nested', *CALYX_RESOURCE_PASSES,
                    '-l', str(calyx.parent.parent/'share/calyx'), '-o', 'kernel.sv'],
                    cwd=root, check=True, capture_output=True, text=True, timeout=60)
                (root/'tb.sv').write_text('''module tb;
reg clk=0,reset=1,go=0; reg[7:0] value=0;
wire[7:0] result,second; wire done; integer index,cycles,tick=0;
main dut(.*); always #5 clk=~clk;
always @(negedge clk)begin
 $display("TRACE %0d %b %b %b",tick,result,second,done);tick=tick+1;
 if(result!==second)$fatal(1,"duplicate outputs disagree");
end
initial begin
 repeat(2)@(negedge clk);reset=0;
 for(index=0;index<256;index=index+1)begin
  go=0;value=255-index;repeat(2)@(negedge clk);
  value=index;go=1;cycles=0;
  while(!done&&cycles<20)begin @(negedge clk);cycles=cycles+1;end
  if(!done||result!==((index+3)&255))$fatal(1,"wrong public result");
  $display("RESULT %0d %0d %0d",index,result,cycles);
  go=0;@(negedge clk);
 end
 value=8'b10x01z00;repeat(3)@(negedge clk);
 value=8'bzzzzzzzz;repeat(3)@(negedge clk);
 reset=1;repeat(3)@(negedge clk);reset=0;
 repeat(3)@(negedge clk);$finish;
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
        self.assertEqual(sum(line.startswith('RESULT ') for line in traces[1].splitlines()),256)
