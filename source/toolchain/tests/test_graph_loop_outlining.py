"""Typed closure outlining preserves loop memory effects and escaped values."""
import os
import subprocess
import unittest
import test_graph_sequential_scratch as scratch_tests

TOOL='llm2fpga-torch-opt'
SOURCE='''module {
func.func @main() -> i64 {
 %zero = arith.constant 0 : index
 %one = arith.constant 1 : index
 %end = arith.constant 16 : index
 %delta = arith.constant 17 : i64
 %initial = arith.constant 9223372036854775799 : i64
 %buffer = memref.alloc() : memref<1xi64>
 memref.store %initial, %buffer[%zero] : memref<1xi64>
 %result = scf.while (%i = %zero) : (index) -> index {
   %continue = arith.cmpi slt, %i, %end : index
   scf.condition(%continue) %i : index
 } do {
 ^bb0(%i : index):
   %old = memref.load %buffer[%zero] : memref<1xi64>
   %new = arith.addi %old, %delta : i64
   memref.store %new, %buffer[%zero] : memref<1xi64>
   %next = arith.addi %i, %one : index
   scf.yield %next : index
 }
 %value = memref.load %buffer[%zero] : memref<1xi64>
 return %value : i64
}}
'''

class LoopOutliningTests(unittest.TestCase):
    def lower(self, source, *, share=False):
        command=[os.environ.get('LLM2FPGA_TORCH_PASS_TOOL',TOOL),'--verify-each']
        if os.environ.get('LLM2FPGA_LOOP_OUTLINE_PLUGIN'):
            command+=['--load-pass-plugin='+os.environ['LLM2FPGA_LOOP_OUTLINE_PLUGIN']]
        passes='llm2fpga-outline-private-loops'
        if share:
            passes+=',llm2fpga-share-private-functions{entrypoint=main}'
        command+=['--pass-pipeline=builtin.module('+passes+')']
        result=subprocess.run(command,input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout
    def test_closure_executes_with_wrapping_memory_effects(self):
        lowered=self.lower(SOURCE)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(SOURCE),-9223372036854775545)
        self.assertEqual(executor.execute(lowered),-9223372036854775545)
        self.assertEqual(executor.execute(self.lower(lowered)),-9223372036854775545)
    def test_escaped_loop_result_remains_in_caller(self):
        source=SOURCE.replace('%value = memref.load %buffer[%zero] : memref<1xi64>',
                              '%value = arith.index_cast %result : index to i64')
        lowered=self.lower(source)
        self.assertEqual(scratch_tests.SequentialScratchTests().execute(lowered),16)

    def test_aliased_capture_keeps_ordered_memory_effects(self):
        source=SOURCE.replace(
            ' %result = scf.while',
            ' %alias = memref.cast %buffer : memref<1xi64> to memref<?xi64>\n'
            ' %result = scf.while').replace(
            'memref.store %new, %buffer[%zero] : memref<1xi64>',
            'memref.store %new, %alias[%zero] : memref<?xi64>')
        lowered=self.lower(source)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),-9223372036854775545)
        self.assertEqual(executor.execute(lowered),-9223372036854775545)

    def test_existing_symbol_is_preserved(self):
        source=SOURCE.replace('module {',
            'module { func.func private @__llm2fpga_loop_0() { return }')
        lowered=self.lower(source)
        self.assertEqual(scratch_tests.SequentialScratchTests().execute(lowered),
                         -9223372036854775545)

    def test_shared_loops_preserve_sequential_updates_to_same_buffer(self):
        start=SOURCE.index(' %result = scf.while')
        end=SOURCE.index(' %value = memref.load')
        second=SOURCE[start:end].replace('%result =','%second =')
        source=SOURCE[:end]+second+SOURCE[end:]
        lowered=self.lower(source,share=True)
        expected=-9223372036854775273
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),expected)
        self.assertEqual(executor.execute(lowered),expected)
