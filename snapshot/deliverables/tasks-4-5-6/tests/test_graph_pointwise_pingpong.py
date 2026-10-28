"""Native SSA coalescing must preserve pointwise ownership/index contracts."""
import os
from pathlib import Path
import re
import subprocess
import unittest


def fixture(*, shape='4', second_map=None, repeated=False, gap='', extra_b='',
            before_a='', after_a='', annotation='', b_alignment=64,
            output_read=False, public_a=False, side_effect=False, dynamic=False,
            reduction=False, multi_output=False, declaration=''):
    rank=len(shape.split('x'));typ='memref<'+shape+'xi64>'
    dims=', '.join('d'+str(i) for i in range(rank));identity='affine_map<('+dims+')->('+dims+')>'
    alternative=second_map or identity;iters=', '.join('"parallel"' for _ in range(rank))
    signature=('%out: '+typ)+(', %a: '+typ if public_a else '')
    alloc_a='' if public_a else '%a = memref.alloc('+('%n' if dynamic else '')+') {alignment = 64 : i64'+annotation+'} : '+typ
    alloc_b='%b = memref.alloc('+('%n' if dynamic else '')+') {alignment = '+str(b_alignment)+' : i64} : '+typ
    more=', %b' if repeated else '';more_types=', '+typ if repeated else ''
    more_args=', %z: i64' if repeated else '';more_maps=', '+alternative if repeated else ''
    effect = 'memref.store %x, %out[%idx] : '+typ if side_effect else ''
    p1=f'''linalg.generic {{indexing_maps = [{identity}, {identity}], iterator_types = [{iters}]}} ins(%a : {typ}) outs(%b : {typ}) {{
      ^bb0(%x: i64, %unused: i64):
        {effect}
        %v = arith.addi %x, %one : i64
        linalg.yield %v : i64
    }}'''
    if reduction:
        input_maps='affine_map<(d0,d1)->(d1)>'
        output_map='affine_map<(d0,d1)->(d0)>'
        second_iters='"parallel", "reduction"'
    else:
        input_maps=(identity if repeated else alternative)+more_maps
        output_map=identity
        second_iters=iters
    outs=f'%a, %out : {typ}, {typ}' if multi_output else f'%a : {typ}'
    extra_init=', %other: i64' if multi_output else ''
    yields='%v2, %v2 : i64, i64' if multi_output else '%v2 : i64'
    p2=f''' linalg.generic {{indexing_maps = [{input_maps}, {output_map}{', '+output_map if multi_output else ''}], iterator_types = [{second_iters}]}} ins(%b{more} : {typ}{more_types}) outs({outs}) {{
      ^bb0(%y: i64{more_args}, %old: i64{extra_init}):
        %v2 = arith.addi %y, {'%old' if output_read else '%z' if repeated else '%one'} : i64
        linalg.yield {yields}
    }}'''
    return f'''module {{
      {declaration}
      func.func @main({signature}) {{
        %zero = arith.constant 0 : i64
        %one = arith.constant 1 : i64
        %idx = arith.constant 0 : index
        %n = arith.constant 4 : index
        {alloc_a}
        {alloc_b}
        linalg.fill ins(%zero : i64) outs(%a : {typ})
        {before_a}
        {p1}
        {gap}
        {p2}
        {extra_b}
        {after_a}
        memref.copy %a, %out : {typ} to {typ}
        return
      }}
    }}'''


class PointwisePingPongTests(unittest.TestCase):
    def check(self, text, expected, before=2):
        tool=os.environ.get('LLM2FPGA_TORCH_PASS_TOOL')
        self.assertTrue(tool and Path(tool).is_file(),'native Torch pass tool required')
        baseline=subprocess.run([tool,'--verify-each'],input=text,text=True,capture_output=True,timeout=30)
        self.assertEqual(baseline.returncode,0,baseline.stderr)
        self.assertEqual(len(re.findall(r'memref.alloc\(',baseline.stdout)),before)
        result=subprocess.run([tool,'--verify-each','--llm2fpga-coalesce-pointwise-pingpong'],input=text,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(re.findall(r'memref.alloc\(',result.stdout)),expected,result.stdout)

    def test_identity_pair_coalesces(self):self.check(fixture(),1)
    def test_repeated_same_element_inputs_coalesce(self):self.check(fixture(repeated=True),1)
    def test_second_map_reading_original_source_is_rejected(self):
        source = fixture(repeated=True).replace('ins(%b, %b :', 'ins(%b, %a :')
        self.check(source, 2)

    def test_native_execution_preserves_wrapping_values(self):
        from test_graph_sequential_scratch import SequentialScratchTests
        values = [-(1 << 63), -1, 0, 1, (1 << 63)-2, (1 << 63)-1]
        for value in values:
            with self.subTest(value=value):
                source = fixture().replace('%zero = arith.constant 0 : i64',
                                           f'%zero = arith.constant {value} : i64')
                # A separate entrypoint calls the actual transformed buffer maps.
                source = source.replace('func.func @main(', 'func.func @maps(')
                expected = ((value + 2 + (1 << 63)) % (1 << 64)) - (1 << 63)
                source = source.rsplit('}', 1)[0] + f"""
                  func.func @main() -> i64 {{
                    %out = memref.alloc() : memref<4xi64>
                    func.call @maps(%out) : (memref<4xi64>) -> ()
                    %idx = arith.constant 3 : index
                    %got = memref.load %out[%idx] : memref<4xi64>
                    return %got : i64
                  }} }}"""
                result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                    '--verify-each', '--llm2fpga-coalesce-pointwise-pingpong'],
                    input=source, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                executor = SequentialScratchTests()
                self.assertEqual(executor.execute(source), expected)
                self.assertEqual(executor.execute(result.stdout), expected)

    def test_static_unit_axis_is_same_element(self):
        self.check(fixture(shape='1x4',repeated=True,second_map='affine_map<(d0,d1)->(0,d1)>'),1)
    def test_nonunit_broadcast_is_rejected(self):
        self.check(fixture(shape='2x4',repeated=True,second_map='affine_map<(d0,d1)->(0,d1)>'),2)
    def test_permuted_access_is_rejected(self):
        self.check(fixture(shape='4x4',second_map='affine_map<(d0,d1)->(d1,d0)>'),2)
    def test_output_init_read_is_rejected(self):self.check(fixture(output_read=True),2)
    def test_extra_intermediate_user_is_rejected(self):
        self.check(fixture(extra_b='memref.copy %b, %out : memref<4xi64> to memref<4xi64>'),2)
    def test_intermediate_alias_is_rejected(self):
        self.check(fixture(extra_b='%view = memref.cast %b : memref<4xi64> to memref<?xi64>'),2)
    def test_earlier_source_alias_is_rejected(self):
        self.check(fixture(before_a='%view = memref.cast %a : memref<4xi64> to memref<?xi64>'),2)
    def test_later_source_alias_is_safe(self):
        self.check(fixture(after_a='%view = memref.cast %a : memref<4xi64> to memref<?xi64>'),1)
    def test_intervening_operation_is_rejected(self):
        self.check(fixture(gap='%gap = arith.constant 7 : i64'),2)
    def test_external_source_is_rejected(self):self.check(fixture(public_a=True),1,before=1)
    def test_alignment_mismatch_is_rejected(self):self.check(fixture(b_alignment=32),2)
    def test_semantic_annotation_is_rejected(self):self.check(fixture(annotation=', fixture.keep'),2)
    def test_captured_write_is_rejected(self):self.check(fixture(side_effect=True),2)
    def test_dynamic_shape_is_rejected(self):self.check(fixture(shape='?',dynamic=True),2)

    def test_reduction_is_rejected(self):self.check(fixture(reduction=True),2)
    def test_multiple_outputs_are_rejected(self):self.check(fixture(multi_output=True),2)
    def test_shifted_access_is_rejected(self):
        self.check(fixture(second_map='affine_map<(d0)->((d0+1) mod 4)>'),2)
    def test_source_escape_is_rejected(self):
        self.check(fixture(declaration='func.func private @escape(memref<4xi64>)',before_a='func.call @escape(%a) : (memref<4xi64>) -> ()'),2)
    def test_element_type_mismatch_is_rejected(self):
        self.check('''module {func.func @main(%out:memref<4xi64>){
          %a=memref.alloc() : memref<4xi64>
          %b=memref.alloc() : memref<4xi32>
          %zero=arith.constant 0 : i64
          linalg.fill ins(%zero:i64) outs(%a:memref<4xi64>)
          linalg.generic {indexing_maps=[affine_map<(d0)->(d0)>,affine_map<(d0)->(d0)>],iterator_types=["parallel"]} ins(%a:memref<4xi64>) outs(%b:memref<4xi32>){
           ^bb0(%x:i64,%unused:i32):
            %t=arith.trunci %x:i64 to i32
            linalg.yield %t:i32
          }
          linalg.generic {indexing_maps=[affine_map<(d0)->(d0)>,affine_map<(d0)->(d0)>],iterator_types=["parallel"]} ins(%b:memref<4xi32>) outs(%a:memref<4xi64>){
           ^bb0(%y:i32,%old:i64):
            %e=arith.extsi %y:i32 to i64
            linalg.yield %e:i64
          }
          memref.copy %a,%out:memref<4xi64> to memref<4xi64>
          return
        }}''',2)

if __name__=='__main__':unittest.main()
