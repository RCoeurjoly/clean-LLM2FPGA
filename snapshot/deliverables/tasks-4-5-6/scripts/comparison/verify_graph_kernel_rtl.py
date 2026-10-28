#!/usr/bin/env python3
"""Capture graph kernels, compile unchanged SV, compare runtime data with PyTorch.

The compiler receives the graph and its input types; test vectors and expected
answers are created afterward. Verilator runs Calyx's native simulation backend
with assertions enabled. This is a kernel gate, not model/board acceptance.
"""
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import torch
from TinyStories.serial_gemv_boundary import serial_gemv
from compiler.frontend.capture import capture_model, import_raw_torch, serialize_raw_torch
from compiler.pipeline import CompilerTool, compile_integer_graph, export_memory_inventory
from compiler.stages import digest


class GemvKernel(torch.nn.Module):
    def forward(self, left, right):
        return serial_gemv(left, right)


class ArgmaxKernel(torch.nn.Module):
    def forward(self, left, right):
        return torch.argmax(left + right)


TESTBENCH = '''module tb;
reg clk=0; always #5 clk=~clk;
reg reset=1; reg go=0; wire done;
main dut(.clk(clk),.reset(reset),.go(go),.done(done));
initial begin
  repeat(4) @(negedge clk); reset=0; go=1;
  wait(done===1'b1); @(negedge clk);
  $display("RESULT %h",dut.mem_2.mem[0]); $finish;
end
initial begin repeat(10000) @(posedge clk); $fatal(1,"timeout"); end
endmodule
'''


def run(command, directory, name, timeout=120):
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    (directory / (name + '.stdout.log')).write_text(result.stdout)
    (directory / (name + '.stderr.log')).write_text(result.stderr)
    if result.returncode:
        raise ValueError(f'{name}: {result.returncode}: {result.stderr}\n{result.stdout}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    for name in ('torch', 'circt', 'memory', 'translate', 'calyx', 'verilator'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--calyx-library', type=Path, required=True)
    parser.add_argument('--task2-source', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    tools = {name: CompilerTool(getattr(args, name).resolve(strict=True))
             for name in ('torch', 'circt', 'memory', 'translate', 'calyx')}
    specification = importlib.util.spec_from_file_location('task2_original_matmul', args.task2_source)
    original = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(original)
    results = []
    for name, model, width, length in (('gemv', GemvKernel(), 64, 4),
                                       ('task2', original.MatmulModule(), 32, 16),
                                       ('argmax', ArgmaxKernel(), 64, 4)):
        directory = args.work_dir / name
        directory.mkdir(parents=True, exist_ok=True)
        dtype = torch.int64 if width == 64 else torch.int32
        shape = (1, length) if name == 'gemv' else (length,)
        examples = (torch.ones(shape, dtype=dtype), torch.ones(shape, dtype=dtype))
        captured = capture_model(model, examples, directory / 'capture')
        exported = torch.export.load(directory / 'capture/model.pt2')
        raw = directory / 'raw.mlir'
        raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
        compiled = compile_integer_graph(raw, directory / 'compile', tools,
                                         calyx_library=args.calyx_library.resolve(strict=True))
        inventory_path, inventory_receipt = export_memory_inventory(
            directory / 'compile/memory-contracts/calyx.mlir',
            directory / 'inventory', tools['memory'])
        memories = [record for record in json.loads(inventory_path.read_text())['memories']
                    if record['physical'] and record['component'] == 'main']
        sv_hash = digest(compiled.output)
        testbench = directory / 'tb.sv'
        testbench.write_text(TESTBENCH)
        build = directory / 'verilator'
        run([str(args.verilator), '--binary', '--timing', '--assert', '-Wno-fatal',
             '--top-module', 'tb', '-Mdir', str(build), '-o', 'simulator',
             str(testbench), str(compiled.output)], directory, 'verilator-build')
        cases = [(list(range(1, length + 1)), list(range(length, 0, -1))),
                 ([-3] * length, list(range(length))),
                 ([2 ** (width - 1) - 1] + [1] * (length - 1), [2] + [-1] * (length - 1))]
        if name == 'argmax':
            minimum, maximum = -(1 << 63), (1 << 63) - 1
            cases += [([minimum] * length, [0] * length),
                      ([minimum, maximum, maximum, minimum], [0] * length)]
        comparisons = []
        for index, (left, right) in enumerate(cases):
            data = directory / f'data-{index}'
            data.mkdir(exist_ok=True)
            # CIRCT's kernel ABI: input args 0/1 and scalar output arg 2.
            # Initialize compiler scratch storage from its typed inventory,
            # rather than assuming every operator needs one allocation.
            for memory in memories:
                cell, bits = memory['cell'], memory['width']
                values = left if cell == 'mem_0' else right if cell == 'mem_1' else [0] * memory['depth']
                if len(values) != memory['depth']:
                    raise ValueError('runtime input differs from native memory geometry')
                (data / f'{cell}.dat').write_text(''.join(
                    f'{value % (1 << bits):0{(bits + 3) // 4}x}\n' for value in values))
            output = run([str(build / 'simulator'), '+DATA=' + str(data)], data, 'simulation', 30)
            tokens = [line.split()[1] for line in output.splitlines() if line.startswith('RESULT ')]
            if len(tokens) != 1:
                raise ValueError('simulation did not produce exactly one result')
            actual = int(tokens[0], 16)
            if actual >= 1 << (width - 1):
                actual -= 1 << width
            tensors = tuple(torch.tensor(values, dtype=dtype).reshape(shape) for values in (left, right))
            expected = int(model(*tensors))
            if actual != expected or digest(compiled.output) != sv_hash:
                raise ValueError(f'{name} case {index}: RTL={actual}, PyTorch={expected}, or SV changed')
            comparisons.append({'left': left, 'right': right, 'rtl': actual, 'pytorch': expected})
        results.append({'kernel': name, 'capture': asdict(captured), 'raw_sha256': digest(raw),
                        'sv_sha256': sv_hash,
                        'inventory_sha256': digest(inventory_path),
                        'stages': [asdict(r) for r in (*compiled.receipts, inventory_receipt)],
                        'cases': comparisons})
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'kernels': results,
        'tool_hashes': {name: digest(getattr(args, name))
                        for name in ('torch', 'circt', 'memory', 'translate', 'calyx', 'verilator')},
        'script_sha256': digest(Path(__file__)), 'task2_source_sha256': digest(args.task2_source),
        'scope': 'Changed-runtime-vector matmul, GEMV and argmax kernel RTL cases; no full-model or board acceptance'}, indent=2) + '\n')
    print(sum(len(result['cases']) for result in results),
          'graph-kernel RTL cases match PyTorch, with native assertions enabled')


if __name__ == '__main__':
    main()
