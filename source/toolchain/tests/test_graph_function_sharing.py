"""Native structural function sharing preserves SSA and capacity semantics."""
import os
import subprocess
import unittest
import test_graph_sequential_scratch as scratch_tests


def fixture():
    return '''module {func.func private @a(%out: memref<4xi64>) {
          %zero = arith.constant 0 : index
          %end = arith.constant 4 : index
          %step = arith.constant 1 : index
          %value = arith.constant 11 : i64
          %one = arith.constant 1 : i64

          %scratch = memref.alloc() : memref<4xi64>

          %result = arith.subi %value, %one : i64
          scf.for %i = %zero to %end step %step {
            memref.store %result, %scratch[%i] : memref<4xi64>
            %v = memref.load %scratch[%i] : memref<4xi64>
            memref.store %v, %out[%i] : memref<4xi64>
          }
          return
        }
func.func private @b(%out: memref<7xi64>) {
          %zero = arith.constant 0 : index
          %end = arith.constant 7 : index
          %step = arith.constant 1 : index
          %value = arith.constant -3 : i64
          %one = arith.constant 1 : i64

          %scratch = memref.alloc() : memref<7xi64>

          %result = arith.subi %value, %one : i64
          scf.for %i = %zero to %end step %step {
            memref.store %result, %scratch[%i] : memref<7xi64>
            %v = memref.load %scratch[%i] : memref<7xi64>
            memref.store %v, %out[%i] : memref<7xi64>
          }
          return
        }func.func @main() -> i64 {
      %zero = arith.constant 0 : index
      %a = memref.alloc() : memref<4xi64>
      %b = memref.alloc() : memref<7xi64>
      func.call @a(%a) : (memref<4xi64>) -> ()
      func.call @b(%b) : (memref<7xi64>) -> ()
      %x = memref.load %a[%zero] : memref<4xi64>
      %y = memref.load %b[%zero] : memref<7xi64>
      %sum = arith.addi %x, %y : i64
      return %sum : i64
    }}'''


class FunctionSharingTests(unittest.TestCase):
    def lower(self,source):
        result=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],'--verify-each',
            '--llm2fpga-share-private-functions=entrypoint=main'],input=source,
            text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout


    def test_different_constant_pooling_preserves_each_use_site(self):
        source='''module {
          func.func private @a(%out: memref<2xi64>) {
            %zero = arith.constant 0 : index
            %one = arith.constant 1 : index
            %pooled = arith.constant 2 : i64
            memref.store %pooled, %out[%zero] : memref<2xi64>
            memref.store %pooled, %out[%one] : memref<2xi64>
            return
          }
          func.func private @b(%out: memref<2xi64>) {
            %zero = arith.constant 0 : index
            %first = arith.constant 3 : i64
            memref.store %first, %out[%zero] : memref<2xi64>
            %second = arith.constant 4 : i64
            %one = arith.constant 1 : index
            memref.store %second, %out[%one] : memref<2xi64>
            return
          }
          func.func @main() -> i64 {
            %zero = arith.constant 0 : index
            %one = arith.constant 1 : index
            %ten = arith.constant 10 : i64
            %a = memref.alloc() : memref<2xi64>
            %b = memref.alloc() : memref<2xi64>
            func.call @a(%a) : (memref<2xi64>) -> ()
            func.call @b(%b) : (memref<2xi64>) -> ()
            %x = memref.load %a[%zero] : memref<2xi64>
            %y = memref.load %a[%one] : memref<2xi64>
            %z = memref.load %b[%zero] : memref<2xi64>
            %w = memref.load %b[%one] : memref<2xi64>
            %x10 = arith.muli %x, %ten : i64
            %xy = arith.addi %x10, %y : i64
            %xy10 = arith.muli %xy, %ten : i64
            %xyz = arith.addi %xy10, %z : i64
            %xyz10 = arith.muli %xyz, %ten : i64
            %result = arith.addi %xyz10, %w : i64
            return %result : i64
          }
        }'''
        lowered=self.lower(source)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),2234)
        self.assertEqual(executor.execute(lowered),2234)
    def test_cpu_execution_before_and_after_sharing(self):
        executor=scratch_tests.SequentialScratchTests()
        for value in (-3,127):
            source=fixture().replace('arith.constant -3 : i64',f'arith.constant {value} : i64')
            self.assertEqual(executor.execute(source),value+9)
            self.assertEqual(executor.execute(self.lower(source)),value+9)
