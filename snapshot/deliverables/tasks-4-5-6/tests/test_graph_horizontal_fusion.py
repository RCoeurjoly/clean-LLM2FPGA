"""Sibling pointwise fusion must retain shared computation once."""
import os
import subprocess
import unittest

FIXTURE = '''module {
func.func @main(%x: tensor<3xi64>) -> (tensor<3xi64>, tensor<3xi64>) attributes {llvm.emit_c_interface} {
 %seven = arith.constant 7 : i64
 %eleven = arith.constant 11 : i64
 %thirteen = arith.constant 13 : i64
 %empty = tensor.empty() : tensor<3xi64>
 %norm = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%x : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %q = arith.divsi %a, %seven : i64
 linalg.yield %q : i64
 } -> tensor<3xi64>
 %left = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%norm : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %v = arith.addi %a, %eleven : i64
 linalg.yield %v : i64
 } -> tensor<3xi64>
 %right = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%norm : tensor<3xi64>) outs(%empty : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %v = arith.muli %a, %thirteen : i64
 linalg.yield %v : i64
 } -> tensor<3xi64>
 return %left, %right : tensor<3xi64>, tensor<3xi64>
} }'''


class HorizontalFusionTests(unittest.TestCase):
    def compile(self, source, extra=()):
        return subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'], '--verify-each',
            '--llm2fpga-fuse-horizontal-pointwise', '--canonicalize',
            '--llm2fpga-fuse-pointwise', '--canonicalize', *extra],
            input=source, text=True, capture_output=True, timeout=120)

    def test_shared_division_is_computed_once_in_one_iteration_domain(self):
        result = self.compile(FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('linalg.generic'), 1)
        self.assertEqual(result.stdout.count('arith.divsi'), 1)
        self.assertIn('tensor<3xi64>, tensor<3xi64>', result.stdout)

    def test_runtime_outputs_and_shared_empty_initializers_match_pytorch(self):
        import ctypes
        import numpy as np
        import torch
        from torch_mlir import ir
        from torch_mlir.execution_engine import ExecutionEngine
        from torch_mlir.runtime import get_ranked_memref_descriptor
        extra = ('--one-shot-bufferize=copy-before-write bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map',
                 '--buffer-results-to-out-params=modify-public-functions hoist-static-allocs',
                 '--convert-linalg-to-loops', '--convert-scf-to-cf', '--convert-cf-to-llvm',
                 '--convert-arith-to-llvm', '--convert-index-to-llvm', '--convert-func-to-llvm',
                 '--finalize-memref-to-llvm', '--reconcile-unrealized-casts')
        result = self.compile(FIXTURE, extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        with ir.Context():
            engine = ExecutionEngine(ir.Module.parse(result.stdout))
            for values in ([7, -8, 0], [-(1 << 63), (1 << 63)-1, -123]):
                x = np.array(values, dtype=np.int64)
                left, right = np.zeros(3, dtype=np.int64), np.zeros(3, dtype=np.int64)
                descriptors = [get_ranked_memref_descriptor(a) for a in (x, left, right)]
                engine.invoke('main', *[ctypes.pointer(ctypes.pointer(d)) for d in descriptors])
                q = torch.div(torch.tensor(values, dtype=torch.int64), 7, rounding_mode='trunc')
                np.testing.assert_array_equal(left, (q + 11).numpy())
                np.testing.assert_array_equal(right, (q * 13).numpy())
                np.testing.assert_array_equal(x, values)

    def test_earlier_result_use_prevents_moving_a_branch(self):
        source = FIXTURE.replace(' %right = linalg.generic',
            ' %zero = arith.constant 0 : index\n'
            ' %early = tensor.extract %left[%zero] : tensor<3xi64>\n'
            ' %right = linalg.generic').replace(
            '%v = arith.muli %a, %thirteen : i64', '%v = arith.muli %a, %early : i64')
        result = self.compile(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('linalg.generic'), 3)

    def test_reduction_is_not_merged_into_a_pointwise_domain(self):
        source = '''module {
func.func @main(%x: tensor<3xi64>) -> (tensor<3xi64>, tensor<3xi64>) {
 %zero = arith.constant 0 : i64
 %one = arith.constant 1 : i64
 %empty = tensor.empty() : tensor<i64>
 %init = linalg.fill ins(%zero : i64) outs(%empty : tensor<i64>) -> tensor<i64>
 %sum = linalg.generic {indexing_maps = [affine_map<(i)->(i)>, affine_map<(i)->()>], iterator_types = ["reduction"]}
 ins(%x : tensor<3xi64>) outs(%init : tensor<i64>) {
 ^bb0(%a: i64, %b: i64):
 %v = arith.addi %a, %b : i64
 linalg.yield %v : i64
 } -> tensor<i64>
 %out = tensor.empty() : tensor<3xi64>
 %left = linalg.generic {indexing_maps = [affine_map<(i)->()>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%sum : tensor<i64>) outs(%out : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %v = arith.addi %a, %one : i64
 linalg.yield %v : i64
 } -> tensor<3xi64>
 %right = linalg.generic {indexing_maps = [affine_map<(i)->()>, affine_map<(i)->(i)>], iterator_types = ["parallel"]}
 ins(%sum : tensor<i64>) outs(%out : tensor<3xi64>) {
 ^bb0(%a: i64, %b: i64):
 %v = arith.muli %a, %a : i64
 linalg.yield %v : i64
 } -> tensor<3xi64>
 return %left, %right : tensor<3xi64>, tensor<3xi64>
} }'''
        result = self.compile(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('linalg.generic'), 2)
        self.assertEqual(result.stdout.count('"reduction"'), 1)
