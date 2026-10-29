"""Capacity pooling preserves private storage, initialization and lifetimes."""
import os
import subprocess
import unittest

class CapacityPoolTests(unittest.TestCase):
    def test_full_width_values_and_row_strides_execute_unchanged(self):
        import test_graph_sequential_scratch as scratch_tests
        source='''module { func.func @main() -> i64 {
        %i0 = arith.constant 0 : index
        %i1 = arith.constant 1 : index
        %low = arith.constant -9223372036854775807 : i64
        %high = arith.constant 9223372036854775807 : i64
        %c17 = arith.constant 17 : i64
        %c29 = arith.constant 29 : i64
        %a = memref.alloc() : memref<4xi64>
        linalg.fill ins(%low : i64) outs(%a : memref<4xi64>)
        %v = memref.load %a[%i0] : memref<4xi64>
        %b = memref.alloc() : memref<2x4xi64>
        linalg.fill ins(%high : i64) outs(%b : memref<2x4xi64>)
        memref.store %c17, %b[%i0,%i1] : memref<2x4xi64>
        memref.store %c29, %b[%i1,%i0] : memref<2x4xi64>
        %x = memref.load %b[%i0,%i1] : memref<2x4xi64>
        %y = memref.load %b[%i1,%i0] : memref<2x4xi64>
        %sum = arith.addi %x, %y : i64
        %answer = arith.xori %v, %sum : i64
        return %answer : i64
        }}'''
        result=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],'--verify-each',
            '--llm2fpga-pool-private-capacity'],input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),-(1<<63)+47)
        folded=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each','--convert-linalg-to-loops','--fold-memref-alias-ops',
            '--canonicalize'],input=result.stdout,text=True,capture_output=True,timeout=30)
        self.assertEqual(folded.returncode,0,folded.stderr)
        self.assertEqual(executor.execute(folded.stdout),-(1<<63)+47)


    def stable_source(self, source, *after):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--llm2fpga-pool-private-capacity=sharing-stable=true',
            *after], input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_stable_bank_choices_allow_capacity_specializations_to_share(self):
        helpers = []
        for name, capacity, values in [('a', 4, (11, -3, 7)), ('b', 8, (20, 21, 22))]:
            helpers.append(f'''func.func private @{name}(%out:memref<1xi64>) {{
              %i=arith.constant 0:index
              %va=arith.constant {values[0]}:i64
              %vb=arith.constant {values[1]}:i64
              %vc=arith.constant {values[2]}:i64
              %a=memref.alloc():memref<{capacity}xi64>
              linalg.fill ins(%va:i64) outs(%a:memref<{capacity}xi64>)
              %b=memref.alloc():memref<8xi64>
              linalg.fill ins(%vb:i64) outs(%b:memref<8xi64>)
              %av=memref.load %a[%i]:memref<{capacity}xi64>
              %bv=memref.load %b[%i]:memref<8xi64>
              %c=memref.alloc():memref<8xi64>
              linalg.fill ins(%vc:i64) outs(%c:memref<8xi64>)
              %cv=memref.load %c[%i]:memref<8xi64>
              %ab=arith.addi %av,%bv:i64
              %sum=arith.addi %ab,%cv:i64
              memref.store %sum,%out[%i]:memref<1xi64>
              return
            }}''')
        source = 'module {' + '\n'.join(helpers) + '''func.func @main()->i64 {
          %i=arith.constant 0:index
          %a=memref.alloc():memref<1xi64>
          %b=memref.alloc():memref<1xi64>
          func.call @a(%a):(memref<1xi64>)->()
          func.call @b(%b):(memref<1xi64>)->()
          %av=memref.load %a[%i]:memref<1xi64>
          %bv=memref.load %b[%i]:memref<1xi64>
          %sum=arith.addi %av,%bv:i64
          return %sum:i64
        }}'''
        lowering = ('--convert-linalg-to-loops', '--fold-memref-alias-ops',
                    '--lower-affine', '--canonicalize', '--scf-for-to-while',
                    '--canonicalize', '--llm2fpga-normalize-index-scales-for-sharing',
                    '--llm2fpga-share-private-functions=entrypoint=main')
        stable = self.stable_source(source, *lowering)
        import test_graph_sequential_scratch as scratch_tests
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source), 78)
        self.assertEqual(executor.execute(stable), 78)

    def test_stable_boolean_pool_preserves_values_and_separates_integer_widths(self):
        source = '''module { func.func @main()->i64 {
          %i=arith.constant 0:index
          %high=arith.constant 9223372036854775807:i64
          %true=arith.constant true
          %false=arith.constant false
          %a=memref.alloc():memref<4xi64>
          linalg.fill ins(%high:i64) outs(%a:memref<4xi64>)
          %av=memref.load %a[%i]:memref<4xi64>
          %b=memref.alloc():memref<4xi1>
          linalg.fill ins(%true:i1) outs(%b:memref<4xi1>)
          %bv=memref.load %b[%i]:memref<4xi1>
          %c=memref.alloc():memref<8xi1>
          linalg.fill ins(%false:i1) outs(%c:memref<8xi1>)
          %cv=memref.load %c[%i]:memref<8xi1>
          %bw=arith.extui %bv:i1 to i64
          %cw=arith.extui %cv:i1 to i64
          %ab=arith.xori %av,%bw:i64
          %answer=arith.xori %ab,%cw:i64
          return %answer:i64
        }}'''
        lowered = self.stable_source(source)
        import test_graph_sequential_scratch as scratch_tests
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source), 9223372036854775806)
        folded = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--convert-linalg-to-loops', '--fold-memref-alias-ops',
            '--canonicalize'], input=lowered, text=True, capture_output=True, timeout=30)
        self.assertEqual(folded.returncode, 0, folded.stderr)
        self.assertEqual(executor.execute(folded.stdout), 9223372036854775806)


if __name__=='__main__':unittest.main()
