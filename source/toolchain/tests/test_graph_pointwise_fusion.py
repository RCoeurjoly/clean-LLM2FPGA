"""Fusion must not repeat pointwise preprocessing inside a MAC reduction."""
import os
import subprocess
import unittest


class PointwiseFusionTests(unittest.TestCase):
    def test_captured_pointwise_outputs_match_pytorch_and_wrapping_reference(self):
        import tempfile
        from pathlib import Path
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import INTEGER_PROFILE, TORCH_INTEGER_PASSES
        class Kernel(torch.nn.Module):
            def __init__(self, values, column):
                super().__init__()
                self.register_parameter('values', torch.nn.Parameter(
                    torch.tensor(values, dtype=torch.int64), requires_grad=False))
                self.column = column
            def forward(self):
                values = torch.div(self.values, 7, rounding_mode='trunc') * 11 + 7
                return values[self.column]
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        runner = os.environ['LLM2FPGA_MLIR_RUNNER']
        passes = ['--llm2fpga-fuse-pointwise' if arg == '--linalg-fuse-elementwise-ops' else arg
                  for name, _, args, _ in INTEGER_PROFILE
                  if name in {'buffers', 'scratch', 'loops', 'structured'} for arg in args]
        pipeline = TORCH_INTEGER_PASSES[:-1] + ',llm2fpga-test-scalar-entry)'
        for values in ([-(1 << 63), -9, (1 << 63)-1], [-(1 << 62), 0, (1 << 62)-1]):
            for column in range(3):
                with self.subTest(values=values, column=column), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    model = Kernel(values, column)
                    raw, standard, llvm = [root / name for name in ('raw.mlir', 'standard.mlir', 'llvm.mlir')]
                    raw.write_text(serialize_raw_torch(import_raw_torch(torch.export.export(model, ()))))
                    commands = [
                        [tool, str(raw), '--pass-pipeline=' + pipeline, '-o', str(standard)],
                        [tool, str(standard), *passes, '--convert-scf-to-cf', '--convert-cf-to-llvm',
                         '--convert-arith-to-llvm', '--convert-index-to-llvm', '--convert-func-to-llvm',
                         '--finalize-memref-to-llvm', '--reconcile-unrealized-casts', '-o', str(llvm)],
                        [runner, str(llvm), '-e', 'main', '--entry-point-result=i64']]
                    for command in commands:
                        result = subprocess.run(command, text=True, capture_output=True, timeout=120)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    value = values[column]
                    quotient = abs(value) // 7 * (-1 if value < 0 else 1)
                    expected = (quotient * 11 + 7 + (1 << 63)) % (1 << 64) - (1 << 63)
                    self.assertEqual(int(result.stdout.strip()), expected)
                    self.assertEqual(int(result.stdout.strip()), int(model()))


    def test_fused_reduction_matches_pytorch_and_wrapping_reference(self):
        import tempfile
        from pathlib import Path
        import torch
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        from compiler.pipeline import INTEGER_PROFILE, TORCH_INTEGER_PASSES
        class Kernel(torch.nn.Module):
            def __init__(self,x,y):
                super().__init__()
                self.register_parameter('x',torch.nn.Parameter(torch.tensor(x,dtype=torch.int64),requires_grad=False))
                self.register_parameter('y',torch.nn.Parameter(torch.tensor(y,dtype=torch.int64),requires_grad=False))
            def forward(self):return (self.x*self.y).sum()
        tool=os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        passes=[arg for name,_,args,_ in INTEGER_PROFILE
                if name in {'buffers','scratch','loops','structured'} for arg in args]
        pipeline=TORCH_INTEGER_PASSES[:-1]+',llm2fpga-test-scalar-entry)'
        for x,y in [([1,-3,7],[11,2,-5]),([-(1<<63),-9,(1<<63)-1],[2,-(1<<63),7])]:
            model=Kernel(x,y)
            expected=(sum(a*b for a,b in zip(x,y))+(1<<63))%(1<<64)-(1<<63)
            self.assertEqual(int(model()),expected)
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);raw=root/'raw.mlir';standard=root/'standard.mlir';llvm=root/'llvm.mlir'
                raw.write_text(serialize_raw_torch(import_raw_torch(torch.export.export(model,()))))
                result=subprocess.run([tool,str(raw),'--pass-pipeline='+pipeline,'-o',str(standard)],text=True,capture_output=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stderr)
                for enabled in [False,True]:
                    selected=passes if enabled else [arg for arg in passes if arg!='--llm2fpga-fuse-pointwise']
                    result=subprocess.run([tool,str(standard),*selected,'--convert-scf-to-cf','--convert-cf-to-llvm',
                        '--convert-arith-to-llvm','--convert-index-to-llvm','--convert-func-to-llvm',
                        '--finalize-memref-to-llvm','--reconcile-unrealized-casts','-o',str(llvm)],text=True,capture_output=True,timeout=120)
                    self.assertEqual(result.returncode,0,result.stderr)
                    result=subprocess.run([os.environ['LLM2FPGA_MLIR_RUNNER'],str(llvm),'-e','main','--entry-point-result=i64'],text=True,capture_output=True,timeout=120)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(int(result.stdout.strip()),expected)
