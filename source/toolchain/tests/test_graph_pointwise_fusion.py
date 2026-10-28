"""Fusion must not repeat pointwise preprocessing inside a MAC reduction."""
import os
import subprocess
import unittest

FIXTURE = '''module {
func.func @project(%x: tensor<4x3xi64>, %w: tensor<6x3xi64>) -> tensor<4x6xi64> {
 %seven = arith.constant 7 : i64
 %zero = arith.constant 0 : i64
 %norm_empty = tensor.empty() : tensor<4x3xi64>
 %norm = linalg.generic {
 indexing_maps = [affine_map<(r,k)->(r,k)>, affine_map<(r,k)->(r,k)>],
 iterator_types = ["parallel", "parallel"]}
 ins(%x : tensor<4x3xi64>) outs(%norm_empty : tensor<4x3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.divsi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<4x3xi64>
 %empty = tensor.empty() : tensor<4x6xi64>
 %init = linalg.fill ins(%zero : i64) outs(%empty : tensor<4x6xi64>) -> tensor<4x6xi64>
 %full = linalg.generic {
 indexing_maps = [affine_map<(r,o,k)->(r,k)>, affine_map<(r,o,k)->(o,k)>, affine_map<(r,o,k)->(r,o)>],
 iterator_types = ["parallel", "parallel", "reduction"]}
 ins(%norm, %w : tensor<4x3xi64>, tensor<6x3xi64>) outs(%init : tensor<4x6xi64>) {
 ^bb0(%a: i64, %b: i64, %c: i64):
 %product = arith.muli %a, %b : i64
 %sum = arith.addi %c, %product : i64
 linalg.yield %sum : i64
 } -> tensor<4x6xi64>
 return %full : tensor<4x6xi64>
} }'''

class PointwiseFusionTests(unittest.TestCase):
    def test_division_remains_outside_mac_reduction(self):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
                                 os.environ.get('LLM2FPGA_FUSION_PASS', '--llm2fpga-fuse-pointwise')],
                                input=FIXTURE, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('linalg.generic'), 2)
        self.assertLess(result.stdout.index('arith.divsi'), result.stdout.index('"reduction"'))

    def run_native(self, fixture):
        result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
                                 '--llm2fpga-fuse-pointwise'], input=fixture,
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_one_to_one_pointwise_chain_fuses(self):
        fixture = '''module {
func.func @chain(%x: tensor<3xi64>) -> tensor<3xi64> {
 %seven = arith.constant 7 : i64
 %empty = tensor.empty() : tensor<3xi64>
 %first = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%x : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.divsi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 %last = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%first : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.addi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 return %last : tensor<3xi64>
} }'''
        self.assertEqual(self.run_native(fixture).count('linalg.generic'), 1)

    def test_broadcast_consumer_keeps_preprocessing_outside_expanded_domain(self):
        fixture = '''module {
func.func @broadcast(%x: tensor<3xi64>) -> tensor<4x3xi64> {
 %seven = arith.constant 7 : i64
 %empty = tensor.empty() : tensor<3xi64>
 %first = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%x : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.divsi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 %expanded = tensor.empty() : tensor<4x3xi64>
 %last = linalg.generic {indexing_maps = [affine_map<(r,i)->(i)>, affine_map<(r,i)->(r,i)>], iterator_types = ["parallel", "parallel"]}
 ins(%first : tensor<3xi64>) outs(%expanded : tensor<4x3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.addi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<4x3xi64>
 return %last : tensor<4x3xi64>
} }'''
        self.assertEqual(self.run_native(fixture).count('linalg.generic'), 2)

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

    def test_shared_preprocessing_is_not_cloned_into_two_consumers(self):
        fixture = '''module {
func.func @fork(%x: tensor<3xi64>) -> (tensor<3xi64>, tensor<3xi64>) {
 %seven = arith.constant 7 : i64
 %empty = tensor.empty() : tensor<3xi64>
 %first = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%x : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.divsi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 %left = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%first : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.addi %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 %right = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%first : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %value = arith.muli %a, %seven : i64
 linalg.yield %value : i64
 } -> tensor<3xi64>
 return %left, %right : tensor<3xi64>, tensor<3xi64>
} }'''
        output = self.run_native(fixture)
        self.assertEqual(output.count('arith.divsi'), 1)
        self.assertEqual(output.count('linalg.generic'), 3)

    def test_one_to_one_product_fuses_into_reduction_without_recomputation(self):
        fixture = '''module {
func.func @dot(%x: tensor<2x3xi64>, %y: tensor<2x3xi64>) -> tensor<2xi64> {
 %zero = arith.constant 0 : i64
 %product_empty = tensor.empty() : tensor<2x3xi64>
 %product = linalg.generic {
 indexing_maps = [affine_map<(r,k)->(r,k)>, affine_map<(r,k)->(r,k)>, affine_map<(r,k)->(r,k)>],
 iterator_types = ["parallel", "parallel"]}
 ins(%x, %y : tensor<2x3xi64>, tensor<2x3xi64>) outs(%product_empty : tensor<2x3xi64>) {
 ^bb0(%a: i64, %b: i64, %old: i64):
 %p = arith.muli %a, %b : i64
 linalg.yield %p : i64
 } -> tensor<2x3xi64>
 %empty = tensor.empty() : tensor<2xi64>
 %init = linalg.fill ins(%zero : i64) outs(%empty : tensor<2xi64>) -> tensor<2xi64>
 %sum = linalg.generic {
 indexing_maps = [affine_map<(r,k)->(r,k)>, affine_map<(r,k)->(r)>],
 iterator_types = ["parallel", "reduction"]}
 ins(%product : tensor<2x3xi64>) outs(%init : tensor<2xi64>) {
 ^bb0(%p: i64, %acc: i64):
 %v = arith.addi %acc, %p : i64
 linalg.yield %v : i64
 } -> tensor<2xi64>
 return %sum : tensor<2xi64>
} }'''
        result = self.run_native(fixture)
        self.assertEqual(result.count('linalg.generic'), 1)
        self.assertIn('"reduction"', result)
        self.assertEqual(result.count('arith.muli'), 1)

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
