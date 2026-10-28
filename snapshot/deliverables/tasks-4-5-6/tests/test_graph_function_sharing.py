"""Native structural function sharing preserves SSA and capacity semantics."""
import os,re,subprocess,unittest
import test_graph_sequential_scratch as scratch_tests


def fixture(*,public=False,observe_capacity=False,changed_ssa=False):
    helpers=[]
    for name,size,value in [('a',4,11),('b',7,-3)]:
        expression=('%left, %right' if name=='a' else '%right, %left') if changed_ssa else '%value, %one'
        derived=('''%left = arith.addi %value, %one : i64
                    %right = arith.muli %value, %one : i64''' if changed_ssa else '')
        observation=(f'%dim = memref.dim %scratch, %zero : memref<{size}xi64>\n'
                     if observe_capacity else '')
        helpers.append(f'''func.func {'' if public else 'private '}@{name}(%out: memref<{size}xi64>) {{
          %zero = arith.constant 0 : index
          %end = arith.constant {size} : index
          %step = arith.constant 1 : index
          %value = arith.constant {value} : i64
          %one = arith.constant 1 : i64
          {derived}
          %scratch = memref.alloc() : memref<{size}xi64>
          {observation}
          %result = arith.subi {expression} : i64
          scf.for %i = %zero to %end step %step {{
            memref.store %result, %scratch[%i] : memref<{size}xi64>
            %v = memref.load %scratch[%i] : memref<{size}xi64>
            memref.store %v, %out[%i] : memref<{size}xi64>
          }}
          return
        }}''')
    return 'module {'+'\n'.join(helpers)+'''func.func @main() -> i64 {
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
    def count(self,source):
        return len(re.findall(r'func.func (?:private )?@(?:a|b)\(',self.lower(source)))
    def test_constants_and_private_capacities_are_parameterized(self):
        lowered=self.lower(fixture())
        self.assertEqual(len(re.findall(r'func.func private @(?:a|b)\(',lowered)),1)
        self.assertFalse(any('memref<4xi64>' in line for line in lowered.splitlines() if 'shared_variants' not in line))
        self.assertIn('llm2fpga.shared_variants',lowered)
    def test_operand_order_difference_is_rejected(self):
        self.assertEqual(self.count(fixture(changed_ssa=True)),2)

    def load_property_fixture(self, property_text='', annotation_text=''):
        return '''module {
          func.func private @a(%input:memref<1xi64>,%out:memref<1xi64>) {
            %zero=arith.constant 0:index
            %value="memref.load"(%input,%zero) ''' + property_text + ' ' + annotation_text + '''
              :(memref<1xi64>,index)->i64
            memref.store %value,%out[%zero]:memref<1xi64>
            return
          }
          func.func private @b(%input:memref<1xi64>,%out:memref<1xi64>) {
            %zero=arith.constant 0:index
            %value="memref.load"(%input,%zero):(memref<1xi64>,index)->i64
            memref.store %value,%out[%zero]:memref<1xi64>
            return
          }
          func.func @main(%input:memref<1xi64>,%out:memref<1xi64>) {
            func.call @a(%input,%out):(memref<1xi64>,memref<1xi64>)->()
            func.call @b(%input,%out):(memref<1xi64>,memref<1xi64>)->()
            return
          }
        }'''

    def test_explicit_false_load_hint_matches_absent_default(self):
        lowered = self.lower(self.load_property_fixture('<{nontemporal = false}>'))
        self.assertEqual(len(re.findall(r'func.func private @', lowered)), 1)

    def test_true_load_hint_and_other_annotations_remain_distinct(self):
        for source in (self.load_property_fixture('<{nontemporal = true}>'),
                       self.load_property_fixture('', '{audit = false}')):
            with self.subTest(source=source):
                lowered = self.lower(source)
                self.assertEqual(len(re.findall(r'func.func private @', lowered)), 2)

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
        self.assertEqual(len(re.findall(r'func.func private @(?:a|b)\(',lowered)),1)
        executor=scratch_tests.SequentialScratchTests()
        self.assertEqual(executor.execute(source),2234)
        self.assertEqual(executor.execute(lowered),2234)
    def test_capacity_observation_is_rejected(self):
        self.assertEqual(self.count(fixture(observe_capacity=True)),2)
    def test_public_functions_are_preserved(self):
        self.assertEqual(self.count(fixture(public=True)),2)
    def test_aliasing_caller_buffer_is_rejected(self):
        source=fixture().replace('func.call @a(%a)',
            '%alias = memref.subview %a[0] [4] [1] : memref<4xi64> to memref<4xi64, strided<[1]>>\nfunc.call @a(%a)')
        self.assertEqual(self.count(source),2)
    def test_nested_calls_are_rejected(self):
        source=fixture().replace('func.call @a(%a)',
            '%yes = arith.constant true\nscf.if %yes { func.call @a(%a)').replace(
            '(memref<4xi64>) -> ()\n      func.call @b','(memref<4xi64>) -> () }\n      func.call @b')
        self.assertEqual(self.count(source),2)
    def test_cpu_execution_before_and_after_sharing(self):
        executor=scratch_tests.SequentialScratchTests()
        for value in (-3,127):
            source=fixture().replace('arith.constant -3 : i64',f'arith.constant {value} : i64')
            self.assertEqual(executor.execute(source),value+9)
            self.assertEqual(executor.execute(self.lower(source)),value+9)

    def test_generation_profile_shares_after_flattening_before_calyx(self):
        from compiler.pipeline import integer_profile
        profile=integer_profile(entrypoint='main')
        names=[stage[0] for stage in profile]
        self.assertEqual(names[names.index('flat-memory')+1],'shared-functions')
        stage=next(stage for stage in profile if stage[0]=='shared-functions')
        self.assertEqual(stage[1],'torch')
        self.assertIn('--llm2fpga-share-private-functions=entrypoint=main',stage[2])
