"""Pool initialized private scratch across synchronous calls through typed IR."""
import os,re,subprocess,tempfile,unittest
from pathlib import Path


def fixture(*,private=True,partial=False,nested=False):
    visibility='private ' if private else ''
    initialization=('memref.store %value, %scratch[%idx] : memref<4xi64>' if partial else
                    'linalg.fill ins(%value : i64) outs(%scratch : memref<4xi64>)')
    calls='func.call @a(%one, %out) : (i64, memref<4xi64>) -> ()\nfunc.call @b(%two, %out) : (i64, memref<4xi64>) -> ()'
    if nested:calls='scf.if %true { '+calls+' }'
    helpers=''
    for name in ('a','b'):
        helpers+=f"""func.func {visibility}@{name}(%value: i64, %out: memref<4xi64>) {{
          %idx = arith.constant 0 : index
          %scratch = memref.alloc() : memref<4xi64>
          {initialization}
          memref.copy %scratch, %out : memref<4xi64> to memref<4xi64>
          return
        }}\n"""
    return 'module { '+helpers+"""func.func @main() -> i64 {
      %one = arith.constant 11 : i64
      %two = arith.constant -7 : i64
      %true = arith.constant true
      %idx = arith.constant 3 : index
      %out = memref.alloc() : memref<4xi64>
      """+calls+"""
      %result = memref.load %out[%idx] : memref<4xi64>
      return %result : i64
    }}"""


class SequentialScratchTests(unittest.TestCase):
    def lower(self,source):
        tool=os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        result=subprocess.run([tool,'--verify-each','--llm2fpga-pool-sequential-scratch=entrypoint=main'],
            input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout
    def allocations(self,source):return len(re.findall(r'memref.alloc\(',self.lower(source)))
    def test_private_sequential_helpers_share_one_scratch(self):
        self.assertEqual(self.allocations(fixture()),2)
    def test_public_helper_interfaces_are_preserved(self):
        self.assertEqual(self.allocations(fixture(private=False)),3)
    def test_partial_initialization_does_not_pool(self):
        self.assertEqual(self.allocations(fixture(partial=True)),3)
    def test_nested_calls_are_conservatively_excluded(self):
        self.assertEqual(self.allocations(fixture(nested=True)),3)
    def test_same_helper_keeps_distinct_concurrent_scratch(self):
        source=fixture().replace('memref.copy %scratch, %out',
            '%other = memref.alloc() : memref<4xi64>\n'
            'linalg.fill ins(%value : i64) outs(%other : memref<4xi64>)\n'
            'memref.copy %scratch, %other : memref<4xi64> to memref<4xi64>\n'
            'memref.copy %other, %out')
        self.assertEqual(self.allocations(source),3)
    def test_aliasing_scratch_is_not_pooled(self):
        source=fixture().replace('memref.copy %scratch, %out',
            '%view = memref.subview %scratch[0] [4] [1] : memref<4xi64> to memref<4xi64, strided<[1]>>\n'
            'memref.copy %view, %out').replace('to memref<4xi64>\n          return',
            'to memref<4xi64>\n          return')
        source=source.replace('memref.copy %view, %out : memref<4xi64>',
            'memref.copy %view, %out : memref<4xi64, strided<[1]>>')
        self.assertEqual(self.allocations(source),3)

    def test_generation_profile_declares_pooling_after_local_reuse(self):
        from compiler.pipeline import integer_profile
        scratch=next(stage for stage in integer_profile(entrypoint='main') if stage[0]=='scratch')
        self.assertEqual(scratch[2][-2:],('--llm2fpga-reuse-buffers','--llm2fpga-pool-sequential-scratch=entrypoint=main'))

    def execute(self,source):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'llvm.mlir'
            command=[os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                '--refback-munge-memref-copy','--convert-linalg-to-loops','--lower-affine',
                '--convert-scf-to-cf','--convert-cf-to-llvm','--convert-arith-to-llvm',
                '--convert-index-to-llvm','--convert-func-to-llvm',
                '--finalize-memref-to-llvm','--reconcile-unrealized-casts','-o',str(output)]
            lowered=subprocess.run(command,input=source,text=True,capture_output=True,timeout=30)
            self.assertEqual(lowered.returncode,0,lowered.stderr)
            result=subprocess.run([os.environ['LLM2FPGA_MLIR_RUNNER'],str(output),'-e','main','--entry-point-result=i64'],
                text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            return int(result.stdout.strip())
    def test_cpu_results_before_and_after_pooling(self):
        for value in (-7,127):
            with self.subTest(value=value):
                source=fixture().replace('arith.constant -7 : i64',f'arith.constant {value} : i64')
                self.assertEqual(self.execute(source),value)
                self.assertEqual(self.execute(self.lower(source)),value)

    def test_single_helper_scratch_is_not_hoisted(self):
        source=fixture().replace('func.call @b(%two, %out) : (i64, memref<4xi64>) -> ()','')
        lowered=self.lower(source)
        signature=next(line for line in lowered.splitlines() if 'func.func private @a(' in line)
        self.assertEqual(signature.count('memref<4xi64>'),1,signature)
