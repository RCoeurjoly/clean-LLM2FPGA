"""Native backend formatting retains large guard-disjointness assertions."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import re
import shutil


class CalyxBackendFormattingTests(unittest.TestCase):
    def emit(self, directory, count, same_value=False, synthesis=False, nested=True):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        library = tool.parent.parent / 'share' / 'calyx'
        source = directory / 'input.futil'
        output = directory / 'output.sv'
        # Distinct values prevent the compiler merging all assignments into one
        # guard. This is authored compiler input, independent of model oracles.
        assignments = '\n'.join(
            f"chosen.in = selector == 16'd{i} ? 32'd{1 if same_value else i};"
            for i in range(count))
        source.write_text('import "primitives/core.futil";\n'
                          'component main(selector: 16) -> (result: 32) {\n'
                          'cells { chosen = std_wire(32); }\n'
                          'wires {\n' + assignments + '\n'
                          "result = chosen.out; done = 1'd1;\n}\ncontrol {}\n}\n")
        options = ['--synthesis'] if synthesis else []
        if synthesis and nested:options.append('--nested')
        subprocess.run([str(tool), str(source), '-b', 'verilog', '-l',
                        str(library), *options, '-o', str(output)], check=True,
                       capture_output=True, text=True, timeout=60)
        return output.read_text()

    def test_nested_merged_guards_keep_frontend_ast_depth_bounded(self):
        with tempfile.TemporaryDirectory() as work:
            directory=Path(work)
            self.emit(directory,512,same_value=True,synthesis=True)
            result=subprocess.run([shutil.which('yosys'),'-p',
                f'read_verilog -sv -dump_ast1 {directory}/output.sv; hierarchy -top main -check'],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            indentation=[len(line)-len(line.lstrip()) for line in result.stdout.splitlines()
                         if 'AST_' in line]
            self.assertTrue(indentation,'native frontend did not expose an AST')
            # Native Yosys dump indents two spaces per parent. This allows64
            # levels, well above log2(512); an unparenthesized OR chain is512.
            self.assertLessEqual(max(indentation),128,'serializer lost balanced guard AST')

    def test_nested_guard_serialization_preserves_four_state_results(self):
        testbench="""module tb;
 reg[15:0]selector=0;wire[31:0]result;wire done;
 integer i,j;reg guard;reg[31:0]expected;
 main dut(.selector(selector),.result(result),.go(1'b0),.clk(1'b0),.reset(1'b0),.done(done));
 task check;
 begin
  #1;guard=0;
  for(j=0;j<512;j=j+1)guard=guard | (selector==16'(j));
  expected=guard ? 32'd1 : 32'd0;
  if(result!==expected)$fatal(1,"guard differs selector=%h result=%h expected=%h",selector,result,expected);
 end
 endtask
 initial begin
  for(i=0;i<514;i=i+1)begin selector=i;check();end
  selector=16'hffff;check();selector=16'hxxxx;check();selector=16'hzzzz;check();
  selector=16'b00000000000000x1;check();selector=16'b10000000000000x1;check();
  $display("PASS four-state known=515 unknown=4");$finish;
 end
endmodule
"""
        for nested in (True,False):
            with self.subTest(nested=nested),tempfile.TemporaryDirectory() as work:
                directory=Path(work)
                self.emit(directory,512,same_value=True,synthesis=True,nested=nested)
                (directory/'tb.sv').write_text(testbench)
                result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',
                    str(directory/'simulator'),str(directory/'output.sv'),str(directory/'tb.sv')],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                result=subprocess.run([shutil.which('vvp'),str(directory/'simulator')],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('PASS four-state known=515 unknown=4',result.stdout)

    def test_synthesis_omits_simulation_guard_check_dependencies(self):
        # More than 128 assignments exercises the shared assertion wires,
        # not only the final procedural assertion suppressed upstream.
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 257, synthesis=True)
        for task in ('$onehot0', '$countones', '$fatal'):
            self.assertFalse(task in rtl, f'Synthesis retains simulation task {task}')
        self.assertIn("selector == 16'd256", rtl)
        self.assertIn('assign result', rtl)

    def test_large_concatenation_has_native_line_breaks(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 25000)
        self.assertIn('$onehot0', rtl)
        self.assertIn('$fatal', rtl)
        # The original serializer produces a 300k-character assertion line.
        # All 25k guards must remain checked by the typed assertion expression.
        assertion = rtl[rtl.index('if(', rtl.index('module main(')):]
        assertion = assertion[:assertion.index('$fatal')]
        # Read-only dependency inspection follows the final check to all guards.
        definitions = dict(re.findall(r'wire (_calyx_guard_check\w*) = ([^;]+);', rtl))
        pending = re.findall(r'\b_calyx_guard_check\w*\b', assertion)
        visited = set()
        expressions = [assertion]
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            expression = definitions[name]
            expressions.append(expression)
            pending.extend(re.findall(r'\b_calyx_guard_check\w*\b', expression))
        assertion = '\n'.join(expressions)
        guards = set(re.findall(r'\b_guard\d+\b', assertion))
        self.assertEqual(len(guards), 25000)
        # Keep every guard checked, while bounding individual assertion vectors.
        vectors = re.findall(r'\{([^{}]+)\}', assertion)
        self.assertTrue(vectors)
        self.assertLessEqual(max(len(re.findall(r'\b_guard\d+\b', vector))
                                 for vector in vectors), 128)
        self.assertLess(max(map(len, assertion.splitlines())), 1000)

    def test_identical_typed_guards_share_native_pool_nodes(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as work:
            source = Path(work)/'input.futil'; output = Path(work)/'output.sv'
            source.write_text('import "primitives/core.futil";\n'
                'component main(selector: 16) -> (first: 32, second: 32) {\n'
                'cells { a = std_wire(32); b = std_wire(32); }\n'
                "wires { a.in = selector == 16'd7 ? 32'd5;\n"
                "b.in = selector == 16'd7 ? 32'd9;\n"
                "first = a.out; second = b.out; done = 1'd1; } control {} }\n")
            subprocess.run([str(tool),str(source),'-b','verilog','-l',
                str(tool.parent.parent/'share/calyx'),'-o',str(output)],check=True,capture_output=True)
            rtl = output.read_text()
        self.assertEqual(rtl.count("selector == 16'd7"), 1)

    def test_large_check_uses_bounded_final_condition(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 25000)
        condition = rtl[rtl.index('if(', rtl.index('module main(')):]
        condition = condition[:condition.index('$fatal')]
        # Bounded vectors alone still duplicate entire guard trees into one
        # enormous C++ function. The final predicate needs shared AST nodes.
        self.assertLess(len(condition), 16384)

    def test_short_concatenation_retains_guard_count(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 3)
        assertion = rtl[rtl.index('if(~$onehot0('):]
        assertion = assertion[:assertion.index(')) begin')]
        self.assertEqual(assertion.count('_guard'), 3)
        self.assertIn('$fatal', rtl)

    def test_large_priority_chain_has_bounded_native_expressions(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 25000)
        assignments = re.findall(r'assign [^;]+;', rtl)
        depth = max(statement.count(' ? ') for statement in assignments)
        self.assertLessEqual(depth, 128)
        self.assertIn('_calyx_priority', rtl)

    def test_merged_boolean_guards_have_bounded_dependency_depth(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = self.emit(Path(work), 25000, same_value=True)
        depth = {}
        for line in rtl.splitlines():
            match = re.fullmatch(r'wire (_guard\d+) = (_guard\d+) [|&] (_guard\d+);', line)
            if match:
                name, left, right = match.groups()
                depth[name] = 1 + max(depth.get(left, 0), depth.get(right, 0))
        self.assertTrue(depth, 'fixture must exercise merged Boolean guards')
        self.assertLessEqual(max(depth.values()), 32)


if __name__ == '__main__':
    unittest.main()
