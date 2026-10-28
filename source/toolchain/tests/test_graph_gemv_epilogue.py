"""Exact scalar GEMV reduction fused with its sole pointwise tensor consumer."""
import os
import subprocess
import unittest


def fixture(*, shared=False, bias=0, output_read=False, divisor=3, flags='', transpose=False, reduction=False):
    init = '%zero' if bias == 0 else '%one'
    maps = ('affine_map<(r,o,k)->(r,k)>, affine_map<(r,o,k)->(k,o)>' if transpose else
            'affine_map<(r,o,k)->(r,k)>, affine_map<(r,o,k)->(o,k)>')
    wtype = 'tensor<3x4xi64>' if transpose else 'tensor<4x3xi64>'
    return f'''module {{ func.func @project(%a:tensor<2x3xi64>, %w:{wtype}, %scale:tensor<4xi64>) -> tensor<2x4xi64> {{
 %zero = arith.constant 0 : i64
 %one = arith.constant 1 : i64
 %divisor = arith.constant {divisor} : i64
 %empty = tensor.empty() : tensor<2x4xi64>
 %init = linalg.fill ins({init}:i64) outs(%empty:tensor<2x4xi64>) -> tensor<2x4xi64>
 %gemv = linalg.generic {{indexing_maps=[{maps},affine_map<(r,o,k)->(r,o)>],iterator_types=["parallel","parallel","reduction"]}}
 ins(%a,%w:tensor<2x3xi64>,{wtype}) outs(%init:tensor<2x4xi64>) {{
 ^bb0(%x:i64,%y:i64,%acc:i64):
  %p = arith.muli %x,%y{flags} : i64
  %v = arith.addi %acc,%p : i64
  linalg.yield %v : i64
 }} -> tensor<2x4xi64>
 %out = linalg.generic {{indexing_maps=[affine_map<(r,o)->(r,o)>,affine_map<(r,o)->(o)>,affine_map<(r,o)->(r,o)>],iterator_types=["parallel","{'reduction' if reduction else 'parallel'}"]}}
 ins(%gemv,%scale:tensor<2x4xi64>,tensor<4xi64>) outs(%empty:tensor<2x4xi64>) {{
 ^bb0(%x:i64,%s:i64,%old:i64):
  %v = arith.muli %x,%s : i64
  %rounded = arith.divsi %v,%divisor : i64
  %answer = arith.addi %rounded,{'%old' if output_read else '%one'} : i64
  linalg.yield %answer : i64
 }} -> tensor<2x4xi64>
 {'%extra = tensor.extract %gemv[%c0,%c0] : tensor<2x4xi64>' if shared else ''}
 return %out : tensor<2x4xi64>
 }} }}'''.replace(' %out = linalg.generic', ' %c0 = arith.constant 0 : index\n %out = linalg.generic')


class GemvEpilogueTests(unittest.TestCase):
    def lower(self, source):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
            '--llm2fpga-fuse-gemv-epilogues', '--canonicalize'],
            input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_reduction_is_scalar_and_intermediate_tensor_disappears(self):
        result = self.lower(fixture())
        self.assertIn('llm2fpga.gemv_epilogue',result)
        self.assertEqual(result.count('scf.for'),1)
        self.assertEqual(result.count('linalg.generic'),1)
        self.assertNotIn('linalg.fill',result)
        self.assertEqual(self.lower(result).count('scf.for'),1)

    def test_shared_nonzero_or_changed_reductions_are_not_fused(self):
        for changes in ({'shared':True},{'bias':1},{'output_read':True},
                        {'flags':' overflow<nsw>'},{'transpose':True}):
            with self.subTest(changes=changes):
                self.assertNotIn('llm2fpga.gemv_epilogue',self.lower(fixture(**changes)))

    def test_full_width_dot_and_rounding_execute_before_and_after_fusion(self):
        from test_graph_sequential_scratch import SequentialScratchTests
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        low,high=-(1<<63),(1<<63)-1
        cases=[([[1,2,3],[-4,5,-6]], [[7,-8,9],[0,1,0],[-1,-2,-3],[2,3,4]], [3,-7,2,1]),
               ([[low,high,-1],[high,low,2]], [[1,1,1],[high,2,-3],[low,-1,2],[3,high,low]], [1,3,-2,high])]
        def wrap(x):return ((x+(1<<63))%(1<<64))-(1<<63)
        for a,w,scales in cases:
          for row,col in ((0,0),(0,1),(0,2),(0,3),(1,0),(1,2),(1,3)):
            dot=0
            for x,y in zip(a[row],w[col]):dot=wrap(dot+wrap(x*y))
            value=wrap(dot*scales[col])
            expected=wrap((abs(value)//3 if value>=0 else -(abs(value)//3))+1)
            source=fixture().rsplit('}',1)[0]+f"""
              func.func @main() -> i64 {{
                %a = arith.constant dense<{a}> : tensor<2x3xi64>
                %w = arith.constant dense<{w}> : tensor<4x3xi64>
                %s = arith.constant dense<{scales}> : tensor<4xi64>
                %r = arith.constant {row} : index
                %c = arith.constant {col} : index
                %out = func.call @project(%a,%w,%s) : (tensor<2x3xi64>,tensor<4x3xi64>,tensor<4xi64>) -> tensor<2x4xi64>
                %v = tensor.extract %out[%r,%c] : tensor<2x4xi64>
                return %v : i64
              }} }}"""
            for selected in (False,True):
              with self.subTest(row=row,col=col,selected=selected,a=a):
                options=['--llm2fpga-fuse-gemv-epilogues'] if selected else []
                result=subprocess.run([tool,'--verify-each',*options,
                  '--linalg-fold-unit-extent-dims','--canonicalize',
                  '--one-shot-bufferize=bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map',
                  '--buffer-results-to-out-params=modify-public-functions hoist-static-allocs'],
                  input=source,text=True,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(SequentialScratchTests().execute(result.stdout),expected)

    def test_slice_unit_folding_forwards_logits_without_private_arrays(self):
        source=fixture().replace('-> tensor<2x4xi64> {','-> tensor<4xi64> {',1)
        source=source.replace('return %out : tensor<2x4xi64>',
            '%slice = tensor.extract_slice %out[1,0] [1,4] [1,1] : tensor<2x4xi64> to tensor<1x4xi64>\n'
            '%vec = tensor.collapse_shape %slice [[0,1]] : tensor<1x4xi64> into tensor<4xi64>\n'
            'return %vec : tensor<4xi64>')
        tool=os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        options=['--verify-each','--llm2fpga-fuse-pointwise','--llm2fpga-fold-producer-slices',
                 '--canonicalize','--llm2fpga-fuse-gemv-epilogues', '--canonicalize',
                 '--linalg-fold-unit-extent-dims','--canonicalize',
                 '--one-shot-bufferize=bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map',
                 '--buffer-results-to-out-params=modify-public-functions hoist-static-allocs']
        result=subprocess.run([tool,*options],input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('memref.alloc()',result.stdout)
        from test_graph_sequential_scratch import SequentialScratchTests
        for index,expected in enumerate((-1,16,-9,-1)):
            main=f"""func.func @main() -> i64 {{
              %a = arith.constant dense<[[1,2,3],[4,5,6]]> : tensor<2x3xi64>
              %w = arith.constant dense<[[1,0,-1],[2,3,4],[-1,-1,-1],[0,0,1]]> : tensor<4x3xi64>
              %s = arith.constant dense<[3,1,2,-1]> : tensor<4xi64>
              %out = func.call @project(%a,%w,%s) : (tensor<2x3xi64>,tensor<4x3xi64>,tensor<4xi64>) -> tensor<4xi64>
              %idx = arith.constant {index} : index
              %v = tensor.extract %out[%idx] : tensor<4xi64>
              return %v : i64
            }}"""
            executable=source.rsplit('}',1)[0]+main+'}'
            lowered=subprocess.run([tool,*options],input=executable,text=True,capture_output=True,timeout=30)
            self.assertEqual(lowered.returncode,0,lowered.stderr)
            folded=subprocess.run([tool,'--verify-each','--convert-linalg-to-loops',
                '--fold-memref-alias-ops','--lower-affine','--canonicalize'],
                input=lowered.stdout,text=True,capture_output=True,timeout=30)
            self.assertEqual(folded.returncode,0,folded.stderr)
            self.assertEqual(SequentialScratchTests().execute(folded.stdout),expected)

    def test_captured_intermediate_stays_immutable_when_output_shape_matches(self):
        source=fixture().replace('4x3','3x3').replace('2x4','2x3').replace('4xi64','3xi64')
        source=source.replace(' %init = linalg.fill',          ' %adjusted = linalg.generic {indexing_maps=[affine_map<(r,k)->(r,k)>,affine_map<(r,k)->(r,k)>],iterator_types=["parallel","parallel"]} ins(%a:tensor<2x3xi64>) outs(%empty:tensor<2x3xi64>) {\n'          ' ^bb0(%x:i64,%old:i64): %v = arith.addi %x,%one : i64\n'          ' linalg.yield %v : i64 } -> tensor<2x3xi64>\n %init = linalg.fill')
        source=source.replace('ins(%a,%w:','ins(%adjusted,%w:')
        tool=os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        from test_graph_sequential_scratch import SequentialScratchTests
        for row,values in enumerate(((14,-12,-4),(26,-27,0))):
          for col,expected in enumerate(values):
            main=f"""func.func @main() -> i64 {{
              %a = arith.constant dense<[[1,2,3],[4,5,6]]> : tensor<2x3xi64>
              %w = arith.constant dense<[[1,2,3],[4,-1,2],[3,2,-4]]> : tensor<3x3xi64>
              %s = arith.constant dense<[2,-3,4]> : tensor<3xi64>
              %out = func.call @project(%a,%w,%s) : (tensor<2x3xi64>,tensor<3x3xi64>,tensor<3xi64>) -> tensor<2x3xi64>
              %r = arith.constant {row} : index
              %c = arith.constant {col} : index
              %v = tensor.extract %out[%r,%c] : tensor<2x3xi64>
              return %v : i64
            }}"""
            for selected in (False,True):
              with self.subTest(row=row,col=col,selected=selected):
                opts=['--llm2fpga-fuse-gemv-epilogues'] if selected else []
                result=subprocess.run([tool,'--verify-each',*opts,'--canonicalize',
                  '--one-shot-bufferize=bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map',
                  '--buffer-results-to-out-params=modify-public-functions hoist-static-allocs',
                  '--convert-linalg-to-loops','--fold-memref-alias-ops','--lower-affine'],
                  input=source.rsplit('}',1)[0]+main+'}',text=True,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(SequentialScratchTests().execute(result.stdout),expected)
