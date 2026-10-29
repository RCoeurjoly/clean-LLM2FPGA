"""Pool initialized private scratch across synchronous calls through typed IR."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def fixture():
    return '''module { func.func private @a(%value: i64, %out: memref<4xi64>) {
          %idx = arith.constant 0 : index
          %scratch = memref.alloc() : memref<4xi64>
          linalg.fill ins(%value : i64) outs(%scratch : memref<4xi64>)
          memref.copy %scratch, %out : memref<4xi64> to memref<4xi64>
          return
        }
func.func private @b(%value: i64, %out: memref<4xi64>) {
          %idx = arith.constant 0 : index
          %scratch = memref.alloc() : memref<4xi64>
          linalg.fill ins(%value : i64) outs(%scratch : memref<4xi64>)
          memref.copy %scratch, %out : memref<4xi64> to memref<4xi64>
          return
        }
func.func @main() -> i64 {
      %one = arith.constant 11 : i64
      %two = arith.constant -7 : i64
      %true = arith.constant true
      %idx = arith.constant 3 : index
      %out = memref.alloc() : memref<4xi64>
      func.call @a(%one, %out) : (i64, memref<4xi64>) -> ()
func.call @b(%two, %out) : (i64, memref<4xi64>) -> ()
      %result = memref.load %out[%idx] : memref<4xi64>
      return %result : i64
    }}'''


class SequentialScratchTests(unittest.TestCase):
    def lower(self,source):
        tool=os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        result=subprocess.run([tool,'--verify-each','--llm2fpga-pool-sequential-scratch=entrypoint=main'],
            input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout


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
