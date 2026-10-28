#!/usr/bin/env python3
"""Replay a graph-derived GEMV with compiler-owned initialized weight memory.

The memory ABI here belongs to this fixed verification kernel, not the model
compiler. Native manifest validation rejects ABI drift. Runtime operands and
CPU answers are produced after hardware compilation.
"""
import argparse
from dataclasses import asdict
import json
import math
from collections import Counter
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import torch
from TinyStories.serial_gemv_boundary import serial_gemv
from compiler.frontend.capture import capture_model, import_raw_torch, serialize_raw_torch
from compiler.pipeline import CompilerTool, compile_integer_graph, bind_initialized_memories, export_memory_inventory
from compiler.stages import digest
from compiler.memory_plan import plan_memories, initializers_from_origins
from compiler.graph_images import materialize_graph_images
from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources


class InitializedGemv(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('weights', torch.tensor([[-3, 5, 2]], dtype=torch.int64))

    def forward(self, left):
        return serial_gemv(left, self.weights)


class ComposedQuantizedGemv(InitializedGemv):
    def __init__(self):
        super().__init__()
        self.register_buffer('scales', torch.tensor([[65536, 131072, 262144]], dtype=torch.int64))

    def forward(self, left):
        from compiler.frontend.fixed_point_ops import activation_qdq
        _, dequantized = activation_qdq(left, self.scales)
        return serial_gemv(dequantized, self.weights)


def testbench(input_cell, output_cell):
    return f'''module tb;
reg clk=0; always #5 clk=~clk;
reg reset=1; reg go=0; wire done;
main dut(.clk(clk),.reset(reset),.go(go),.done(done));
initial begin
  $readmemh("inputs.hex", dut.{input_cell}.mem);
  repeat(4) @(negedge clk); reset=0; go=1;
  wait(done===1'b1); @(negedge clk);
  $display("RESULT %h",dut.{output_cell}.mem[0]); $finish;
end
initial begin repeat(10000) @(posedge clk); $fatal(1,"timeout"); end
endmodule
'''


def run(command, root, label, timeout=120):
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=timeout)
    (root / (label + '.stdout.log')).write_text(result.stdout)
    (root / (label + '.stderr.log')).write_text(result.stderr)
    if result.returncode:
        raise ValueError(f'{label}: {result.returncode}: {result.stderr}\n{result.stdout}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    for name in ('torch', 'circt', 'memory', 'translate', 'calyx', 'verilator', 'yosys'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--calyx-library', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--composed', action='store_true')
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    tools = {name: CompilerTool(getattr(args, name).resolve(strict=True))
             for name in ('torch', 'circt', 'memory', 'translate', 'calyx')}
    model = ComposedQuantizedGemv() if args.composed else InitializedGemv()
    capture = capture_model(model, (torch.ones((1, 3), dtype=torch.int64),), root / 'capture')
    exported = torch.export.load(root / 'capture/model.pt2')
    kinds = [spec.kind.name for spec in exported.graph_signature.input_specs]
    if kinds != (['BUFFER', 'BUFFER', 'USER_INPUT'] if args.composed else ['BUFFER', 'USER_INPUT']):
        raise ValueError('verification kernel graph signature changed')
    raw = root / 'raw.mlir'
    raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
    core = compile_integer_graph(raw, root / 'core', tools, target='memory-contracts',
                                 graph_inputs=root / 'capture/graph-inputs.json')
    graph_inputs = json.loads((root / 'capture/graph-inputs.json').read_text())
    images = materialize_graph_images(exported, graph_inputs, root)
    image = images['weights']
    declarations = emit_memory_sources([
        MemorySpec('buffer_' + str(record['index']),
                   64 if record['dtype'] == 'torch.int64' else 32,
                   math.prod(record['shape']), 'bram', 2, False,
                   images[record['target']].name)
        for record in graph_inputs['inputs'] if record['kind'] != 'USER_INPUT'])
    primitive = root / 'closure_memory.sv'
    primitive.write_text(declarations['sv'])
    inventory_path, inventory_receipt = export_memory_inventory(
        core.output, root / 'inventory', tools['memory'])
    # Frozen-buffer binding follows graph identity and typed reference edges.
    inventory = json.loads(inventory_path.read_text())
    origins = {cell['origin']['target']: cell['origin']['sha256']
               for cell in inventory['memories']
               if cell.get('origin', {}).get('kind') == 'BUFFER'}
    if origins != {target: capture.state_identities[target]['sha256'] for target in images}:
        raise ValueError('captured buffer origins were lost or changed during native lowering')
    document = plan_memories(inventory,
        initializers_from_origins(inventory, images))
    manifest = root / 'bindings.json'
    manifest.write_text(json.dumps(document, indent=2) + '\n')
    initialized = bind_initialized_memories(core.output, manifest, primitive,
        root / 'initialized', tools, calyx_library=args.calyx_library.resolve(strict=True))
    sv_hash = digest(initialized.output)
    # This verification-only kernel ABI puts the single runtime operand after
    # its frozen arguments and the scalar output after all input arguments.
    testbench_path = root / 'tb.sv'
    testbench_path.write_text(testbench('mem_' + str(len(kinds) - 1), 'mem_' + str(len(kinds))))
    build = root / 'verilator'
    run([str(args.verilator), '--binary', '--timing', '--assert', '-Wno-fatal',
         '--top-module', 'tb', '-Mdir', str(build), '-o', 'simulator',
         str(testbench_path), str(initialized.output)], root, 'verilator-build')
    comparisons = []
    cases = ([65536, -131072, 196608], [128, -256, 512],
             [-(1 << 31), (1 << 31) - 1, -257]) if args.composed else (
             [1, 2, 3], [-4, 0, 7], [(1 << 63) - 1, -(1 << 63), 9])
    for index, values in enumerate(cases):
        (root / 'inputs.hex').write_text(''.join(f'{value % (1 << 64):016x}\n' for value in values))
        stdout = run([str(build / 'simulator')], root, f'simulation-{index}', 30)
        answers = [line.split()[1] for line in stdout.splitlines() if line.startswith('RESULT ')]
        if len(answers) != 1:
            raise ValueError('simulation must return exactly one result')
        actual = int(answers[0], 16)
        if actual >= (1 << 63):
            actual -= 1 << 64
        expected = int(model(torch.tensor([values], dtype=torch.int64)))
        if actual != expected or digest(initialized.output) != sv_hash:
            raise ValueError(f'case {index}: RTL={actual}, PyTorch={expected}, or hardware changed')
        comparisons.append({'input': values, 'rtl': actual, 'pytorch': expected})
    synthesis = bind_initialized_memories(core.output, manifest, primitive,
        root / 'synthesis', tools, calyx_library=args.calyx_library.resolve(strict=True),
        synthesis=True)
    # Yosys reads compiler-emitted synthesis SV directly. No frontend rewrite.
    if any(character in str(synthesis.output) for character in ('"', '\\', '\n')):
        raise ValueError('unsupported Yosys input path')
    yosys_script = ('read_verilog -sv -D SYNTHESIS "' + str(synthesis.output) +
                    '"; hierarchy -top main; proc; memory_collect; write_json graph-memory.json')
    run([str(args.yosys), '-Q', '-T', '-p', yosys_script], root, 'yosys-memory')
    netlist = root / 'graph-memory.json'
    design = json.loads(netlist.read_text())
    initialized_memories = [cell for module in design['modules'].values()
                            for cell in module['cells'].values()
                            if cell['type'] == '$mem_v2' and
                            'x' not in cell['parameters']['INIT'].lower()]
    actual_words = Counter((int(cell['parameters']['WIDTH'], 2),
                            int(cell['parameters']['SIZE'], 2),
                            int(cell['parameters']['INIT'], 2)) for cell in initialized_memories)
    expected_words = Counter()
    for target in images:
        tensor = exported.state_dict[target]
        width = tensor.element_size() * 8
        words = sum((int(value) % (1 << width)) << (width * index)
                    for index, value in enumerate(tensor.flatten()))
        expected_words[(width, tensor.numel(), words)] += 1
    if actual_words != expected_words:
        raise ValueError('Yosys initialized memories differ from captured graph buffers')
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'capture': asdict(capture),
        'raw_sha256': digest(raw), 'inventory_sha256': digest(inventory_path), 'manifest_sha256': digest(manifest),
        'image_sha256': digest(image), 'primitive_sha256': digest(primitive),
        'sv_sha256': sv_hash, 'script_sha256': digest(Path(__file__)),
        'tool_hashes': {name: digest(getattr(args, name))
                        for name in ('torch', 'circt', 'memory', 'translate', 'calyx', 'verilator', 'yosys')},
        'stages': [asdict(receipt) for receipt in (*core.receipts, inventory_receipt, *initialized.receipts, *synthesis.receipts)],
        'synthesis_sv_sha256': digest(synthesis.output),
        'yosys_netlist_sha256': digest(netlist),
        'cases': comparisons,
        'scope': ('composed Q/DQ + GEMV' if args.composed else 'initialized GEMV') +
                 ' across component references; not full-model/board acceptance'},
        indent=2) + '\n')
    print('3 ' + ('composed Q/DQ + GEMV' if args.composed else 'initialized GEMV') + ' RTL cases match PyTorch')


if __name__ == '__main__':
    main()
