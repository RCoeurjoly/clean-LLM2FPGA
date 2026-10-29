"""Bounds-checked native row-major flattening beyond rank two."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path


def memref(shape):return 'memref<'+''.join(f'{n}x' for n in shape)+'i64>'

def fixture(shape,index):
    type_=memref(shape)
    constants='\n'.join(f'%i{i} = arith.constant {n} : index' for i,n in enumerate(index))
    indices=', '.join(f'%i{i}' for i in range(len(index)))
    return f'''module {{
      func.func private @read(%buffer: {type_}) -> i64 {{
        {constants}
        %v = memref.load %buffer[{indices}] : {type_}
        return %v : i64
      }}
      func.func @main() -> i64 {{
        {constants}
        %buffer = memref.alloc() : {type_}
        %value = arith.constant 45 : i64
        memref.store %value, %buffer[{indices}] : {type_}
        %result = func.call @read(%buffer) : ({type_}) -> i64
        return %result : i64
      }}
    }}'''


class MemoryFlattenTests(unittest.TestCase):
    def lower(self,source):
        result=subprocess.run([os.environ['LLM2FPGA_MEMORY_TOOL'],'--verify-each',
            '--flatten-memref','--canonicalize'],input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout
    def execute(self,source):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'llvm.mlir'
            result=subprocess.run([os.environ['LLM2FPGA_FLATTEN_MLIR_TOOL'],
                '--verify-each','--generate-runtime-verification','--convert-scf-to-cf',
                '--convert-cf-to-llvm','--convert-arith-to-llvm','--convert-index-to-llvm',
                '--convert-func-to-llvm','--finalize-memref-to-llvm',
                '--reconcile-unrealized-casts','-o',str(output)],
                input=source,text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run([os.environ['LLM2FPGA_FLATTEN_MLIR_RUNNER'],str(output),
                '-e','main','--entry-point-result=i64'],text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr[:2000])
            return int(result.stdout.strip())
    def test_allocations_calls_loads_and_stores_preserve_valid_indices(self):
        for shape,index in [([],[]),([7],[6]),([3,5],[2,4]),([2,3,4],[1,2,3]),
                            ([4,4,16,4],[1,0,0,0]),([4,4,16,4],[3,3,15,3])]:
            with self.subTest(shape=shape,index=index):
                source=fixture(shape,index)
                self.assertEqual(self.execute(source),45)
                self.assertEqual(self.execute(self.lower(source)),45)
    def test_flattened_globals_use_row_major_offsets(self):
        values=[[[i*12+j*4+k for k in range(4)] for j in range(3)] for i in range(2)]
        for index in [(0,1,2),(1,0,0),(1,2,3)]:
            with self.subTest(index=index):
                constants='\n'.join(f'%i{i} = arith.constant {n} : index' for i,n in enumerate(index))
                indices=', '.join(f'%i{i}' for i in range(3))
                source=f'''module {{
                  memref.global "private" constant @data : memref<2x3x4xi64> = dense<{json.dumps(values)}>
                  func.func @main() -> i64 {{
                    {constants}
                    %buffer = memref.get_global @data : memref<2x3x4xi64>
                    %v = memref.load %buffer[{indices}] : memref<2x3x4xi64>
                    return %v : i64
                  }}
                }}'''
                expected=index[0]*12+index[1]*4+index[2]
                self.assertEqual(self.execute(source),expected)
                self.assertEqual(self.execute(self.lower(source)),expected)
