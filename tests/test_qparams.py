import ctypes
import os
from pathlib import Path
import resource
import signal
import subprocess
import tempfile
import unittest

import numpy as np
import torch
from torch_mlir import fx, ir
from torch_mlir.execution_engine import ExecutionEngine
from torch_mlir.runtime import get_ranked_memref_descriptor

from extract_operation import extract_operation
from model.import_torch import quantization_decompositions


def run(*args):
    subprocess.run(args, check=True, timeout=180)


class QparamsTests(unittest.TestCase):
    @torch.no_grad()
    def test_full_model_decomposition_is_exact(self):
        torch.set_num_threads(1)
        program = torch.export.load(os.environ['QUANTIZED_MODEL'])
        expected = program.module()(*program.example_inputs[0])
        table = fx.get_decomposition_table() | quantization_decompositions()
        actual = program.run_decompositions(table).module()(*program.example_inputs[0])
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)

    @torch.no_grad()
    def test_model_operation_matches_pytorch_after_lowering(self):
        torch.set_num_threads(1)
        program = torch.export.load(os.environ['QUANTIZED_MODEL'])
        node = next(node for node in program.graph.nodes
                    if str(node.target) == 'quantized_decomposed.choose_qparams.tensor')
        case, arguments, expected = extract_operation(program, node.name)
        torch.testing.assert_close(case(*arguments), expected, rtol=0, atol=0)
        exported = torch.export.export(case, arguments, strict=False)
        table = fx.get_decomposition_table() | quantization_decompositions()
        module = fx.export_and_import(exported, decomposition_table=table)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, linalg, llvm = [root / name for name in ('raw.mlir', 'linalg.mlir', 'llvm.mlir')]
            raw.write_text(str(module))
            run(os.environ['TORCH_OPT'], str(raw),
                '--torchdynamo-export-to-torch-backend-pipeline',
                '--torch-backend-to-linalg-on-tensors-backend-pipeline',
                '--canonicalize', '-o', str(linalg))
            run(os.environ['MLIR_OPT'], str(linalg), '--empty-tensor-to-alloc-tensor',
                '--one-shot-bufferize=bufferize-function-boundaries '
                'function-boundary-type-conversion=identity-layout-map',
                '--buffer-results-to-out-params=modify-public-functions hoist-static-allocs',
                '--convert-bufferization-to-memref', '--convert-linalg-to-loops',
                '--lower-affine', '--convert-scf-to-cf', '--convert-math-to-llvm',
                '--llvm-request-c-wrappers', '--convert-arith-to-llvm',
                '--convert-index-to-llvm', '--convert-func-to-llvm',
                '--finalize-memref-to-llvm', '--convert-cf-to-llvm',
                '--reconcile-unrealized-casts', '-o', str(llvm))
            with ir.Context():
                engine = ExecutionEngine(ir.Module.parse(llvm.read_text()))
                values = arguments[0]
                eps = node.args[3]
                inputs = [values, torch.zeros_like(values), torch.ones_like(values),
                          -torch.ones_like(values)]
                inputs += [torch.linspace(-factor, 2 * factor, values.numel()).reshape_as(values)
                           for factor in (eps / 10, eps, 1., 1e6)]
                inputs += [torch.full_like(values, -eps * factor) for factor in (.5, 1.5, 2.5)]
                generator = torch.Generator().manual_seed(14)
                inputs += [torch.randn(values.shape, generator=generator) * factor
                           for factor in (1e-8, .01, 1., 100., 1e6)]
                for value in inputs:
                    expected = case(value)
                    arrays = [value.numpy().copy()] + [np.zeros_like(x.numpy()) for x in expected]
                    descriptors = [ctypes.pointer(ctypes.pointer(get_ranked_memref_descriptor(x)))
                                   for x in arrays]
                    engine.invoke('main', *descriptors)
                    for actual, wanted in zip(arrays[1:], expected):
                        np.testing.assert_array_equal(actual, wanted.numpy())
                bad = torch.full_like(values, float('nan'))
                with self.assertRaises(AssertionError):
                    case(bad)
                arrays[0][:] = bad.numpy()
                pid = os.fork()
                if pid == 0:
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                    engine.invoke('main', *descriptors)
                    os._exit(0)
                _, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGABRT)


if __name__ == '__main__':
    unittest.main()
