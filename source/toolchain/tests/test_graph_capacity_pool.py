"""Capacity pooling preserves private storage, initialization and lifetimes."""
import os,re,subprocess,unittest

PREFIX='''module { func.func @main(%out: memref<8xi64>) {
%zero = arith.constant 0 : i64
%one = arith.constant 1 : i64
%i = arith.constant 0 : index
'''
class CapacityPoolTests(unittest.TestCase):
    def lower(self,body):
        result=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],'--verify-each','--llm2fpga-pool-private-capacity'],input=PREFIX+body+'\nreturn\n}}',text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout
    def count(self,body):return len(re.findall(r' = memref.alloc\(',self.lower(body)))
    a='''%a = memref.alloc() : memref<4xi64>
linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)
%av = memref.expand_shape %a [[0, 1]] output_shape [1, 4] : memref<4xi64> into memref<1x4xi64>
%ac = memref.collapse_shape %av [[0, 1]] : memref<1x4xi64> into memref<4xi64>
%v = memref.load %ac[%i] : memref<4xi64>
'''
    b='''%b = memref.alloc() : memref<2x4xi64>
linalg.fill ins(%one : i64) outs(%b : memref<2x4xi64>)
%w = memref.load %b[%i,%i] : memref<2x4xi64>
memref.store %w, %out[%i] : memref<8xi64>
'''
    def test_disjoint_different_capacities_share_one_full_width_bank(self):
        lowered=self.lower(self.a+self.b)
        self.assertEqual(len(re.findall(r' = memref.alloc\(',lowered)),1)
        self.assertIn('memref<8xi64>',lowered)
    def test_late_alias_read_keeps_overlapping_banks_separate(self):
        self.assertEqual(self.count(self.a+self.b+'%late = memref.load %ac[%i] : memref<4xi64>'),2)
    def test_partial_first_write_is_not_eligible(self):
        body=self.b.replace('linalg.fill ins(%one : i64) outs(%b : memref<2x4xi64>)','memref.store %one, %b[%i,%i] : memref<2x4xi64>')
        self.assertEqual(self.count(self.a+body),2)
    def test_freed_alias_does_not_enter_a_pool(self):
        self.assertEqual(self.count(self.a+'memref.dealloc %ac : memref<4xi64>\n'+self.b),2)
    def test_annotation_preserves_storage_identity(self):
        self.assertEqual(self.count(self.a.replace('memref.alloc() : memref<4xi64>','memref.alloc() {keep_identity} : memref<4xi64>')+self.b),2)
    def test_escaping_call_does_not_enter_a_pool(self):
        source=PREFIX+self.a+'func.call @escape(%ac) : (memref<4xi64>) -> ()\n'+self.b+'return\n} func.func private @escape(memref<4xi64>) }'
        result=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],'--verify-each','--llm2fpga-pool-private-capacity'],input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(re.findall(r' = memref.alloc\(',result.stdout)),2)
    def test_alignment_is_at_least_each_original_requirement(self):
        source=self.a.replace('memref.alloc() : memref<4xi64>','memref.alloc() {alignment = 64 : i64} : memref<4xi64>')+self.b.replace('memref.alloc() : memref<2x4xi64>','memref.alloc() {alignment = 128 : i64} : memref<2x4xi64>')
        lowered=self.lower(source);self.assertEqual(len(re.findall(r' = memref.alloc\(',lowered)),1);self.assertIn('alignment = 128',lowered)

    def test_typed_views_lower_to_existing_memory_accesses(self):
        lowered=self.lower(self.a+self.b)
        result=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],'--verify-each',
            '--convert-linalg-to-loops','--fold-memref-alias-ops','--canonicalize'],
            input=lowered,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        for operation in ['memref.reinterpret_cast','memref.subview','memref.expand_shape','memref.collapse_shape']:
            self.assertNotIn(operation,result.stdout)
    def test_rank_zero_scratch_uses_a_bounded_scalar_view(self):
        scalar='''%scalar = memref.alloc() : memref<i64>
        linalg.fill ins(%one : i64) outs(%scalar : memref<i64>)
        %value = memref.load %scalar[] : memref<i64>
        memref.store %value, %out[%i] : memref<8xi64>'''
        self.assertEqual(self.count(self.a+scalar),1)
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
        self.assertEqual(len(re.findall(r' = memref.alloc\(',result.stdout)),1)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),-(1<<63)+47)
        folded=subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each','--convert-linalg-to-loops','--fold-memref-alias-ops',
            '--canonicalize'],input=result.stdout,text=True,capture_output=True,timeout=30)
        self.assertEqual(folded.returncode,0,folded.stderr)
        self.assertNotIn('memref.subview',folded.stdout)
        self.assertEqual(executor.execute(folded.stdout),-(1<<63)+47)

    def test_copy_source_and_destination_lifetimes_overlap(self):
        body=self.a+"""%b = memref.alloc() : memref<4xi64>
        memref.copy %a,%b : memref<4xi64> to memref<4xi64>
        %w = memref.load %b[%i] : memref<4xi64>
        memref.store %w,%out[%i] : memref<8xi64>"""
        self.assertEqual(self.count(body),2)

    def test_unproved_subview_alias_remains_separate(self):
        body=self.a+'%view = memref.subview %a[0] [4] [1] : memref<4xi64> to memref<4xi64>\n'+self.b
        self.assertEqual(self.count(body),2)

    def test_other_element_width_does_not_enter_i64_pool(self):
        body=self.a+"""%small = arith.constant 3 : i32
        %b = memref.alloc() : memref<8xi32>
        linalg.fill ins(%small:i32) outs(%b:memref<8xi32>)
        %w = memref.load %b[%i] : memref<8xi32>"""
        self.assertEqual(self.count(body),2)

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
        default = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--llm2fpga-pool-private-capacity', *lowering],
            input=source, text=True, capture_output=True, timeout=30)
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(len(re.findall(r'func.func private @', default.stdout)), 2)
        stable = self.stable_source(source, *lowering)
        self.assertEqual(len(re.findall(r'func.func private @', stable)), 1)
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
        self.assertEqual(len(re.findall(r' = memref.alloc\(', lowered)), 2)
        self.assertIn('memref<8xi1>', lowered)
        import test_graph_sequential_scratch as scratch_tests
        executor = scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source), 9223372036854775806)
        folded = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
            '--verify-each', '--convert-linalg-to-loops', '--fold-memref-alias-ops',
            '--canonicalize'], input=lowered, text=True, capture_output=True, timeout=30)
        self.assertEqual(folded.returncode, 0, folded.stderr)
        self.assertEqual(executor.execute(folded.stdout), 9223372036854775806)

    def test_stable_pool_rejects_unproved_initialization_aliases_and_overlap(self):
        partial = self.b.replace(
            'linalg.fill ins(%one : i64) outs(%b : memref<2x4xi64>)',
            'memref.store %one, %b[%i,%i] : memref<2x4xi64>')
        late = '%late = memref.load %ac[%i] : memref<4xi64>'
        view = '%view = memref.subview %a[0] [4] [1] : memref<4xi64> to memref<4xi64>\n'
        for body in (self.a + partial, self.a + self.b + late,
                     self.a + view + self.b, self.a + 'memref.dealloc %ac : memref<4xi64>\n' + self.b):
            with self.subTest(body=body):
                lowered = self.stable_source(PREFIX + body + '\nreturn\n}}')
                self.assertEqual(len(re.findall(r' = memref.alloc\(', lowered)), 2)

if __name__=='__main__':unittest.main()
