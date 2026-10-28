#!/usr/bin/env python3
"""Verify native ROM narrowing, read completion and actual Yosys storage width.

This is a verification-only primitive fixture, not a model compiler input.
The native pass proves each image word survives the declared extension.
"""
import argparse
from dataclasses import asdict
from itertools import product
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler.pipeline import CompilerTool, bind_initialized_memories
from compiler.stages import digest
from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources


def source(address):
    return f'''module attributes {{calyx.entrypoint = "main"}} {{
 calyx.component @main(%clk: i1 {{clk}}, %reset: i1 {{reset}}, %go: i1 {{go}}) -> (%done: i1 {{done}}, %value: i64) {{
  %false = hw.constant false
  %true = hw.constant true
  %addr = hw.constant {address} : i2
  %zero = hw.constant 0 : i64
  %rom.addr0, %rom.clk, %rom.reset, %rom.content_en, %rom.write_en, %rom.write_data, %rom.read_data, %rom.done = calyx.seq_mem @rom <[3] x 64> [2] {{external = true}} : i2, i1, i1, i1, i1, i64, i64, i1
  %result.in, %result.write_en, %result.clk, %result.reset, %result.out, %result.done = calyx.register @result : i64, i1, i1, i1, i64, i1
  calyx.wires {{
   calyx.assign %value = %result.out : i64
   calyx.group @read {{
    calyx.assign %rom.addr0 = %addr : i2
    calyx.assign %rom.content_en = %true : i1
    calyx.assign %rom.write_en = %false : i1
    calyx.assign %rom.write_data = %zero : i64
    calyx.group_done %rom.done : i1
   }}
   calyx.group @record {{
    calyx.assign %result.in = %rom.read_data : i64
    calyx.assign %result.write_en = %true : i1
    calyx.group_done %result.done : i1
   }}
  }}
  calyx.control {{calyx.seq {{calyx.enable @read calyx.enable @record}}}}
 }} {{toplevel}}
}}'''


TB = '''module tb;
reg clk=0; always #5 clk=~clk;
reg reset=1; reg go=0; wire done; wire [63:0] value;
main dut(.clk(clk),.reset(reset),.go(go),.done(done),.value(value));
initial begin
 repeat(4) @(negedge clk); reset=0; go=1;
 wait(done===1'b1); @(negedge clk);
 $display("RESULT %h",value); $finish;
end
initial begin repeat(1000) @(posedge clk); $fatal(1,"timeout"); end
endmodule
'''


def run(argv, directory, label):
    result = subprocess.run(argv, cwd=directory, text=True, capture_output=True, timeout=120)
    (directory / (label + '.stdout.log')).write_text(result.stdout)
    (directory / (label + '.stderr.log')).write_text(result.stderr)
    if result.returncode:
        raise ValueError(f'{label} failed: see {directory}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    for name in ('memory', 'translate', 'calyx', 'calyx-library', 'verilator', 'yosys', 'work-dir', 'receipt'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    args.receipt.unlink(missing_ok=True)
    tools = {name: CompilerTool(getattr(args, name).resolve(strict=True))
             for name in ('memory', 'translate', 'calyx')}
    cases = []
    for kind, (signed, words) in product(('register', 'bram', 'lutram'),
            ((True, [-128, -1, 127]), (False, [0, 128, 255]))):
        for address in range(3):
            root = args.work_dir.resolve() / f'{kind}-{"signed" if signed else "unsigned"}-{address}'
            root.mkdir(parents=True, exist_ok=True)
            ir = root / 'source.mlir'
            ir.write_text(source(address))
            image = root / 'weights.hex'
            image.write_text(''.join(f'{word % (1 << 64):016x}\n' for word in words))
            manifest = root / 'bindings.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'bindings': [dict(
                component='main', cell='rom', width=64, depth=3, address_width=2,
                implementation=kind, read_latency=1 if kind=='register' else 2, writable=False,
                init_file=image.name, init_sha256=digest(image),
                storage_width=8, sign_extend=signed)]}, indent=2)+'\n')
            primitive = root / 'closure_memory.sv'
            primitive.write_text(emit_memory_sources([
                MemorySpec('fixture', 64, 3, kind, 1 if kind=='register' else 2, False, image.name)],
                include_narrow_rom=True)['sv'])
            compiled = bind_initialized_memories(ir, manifest, primitive,
                root / 'compiled', tools, calyx_library=args.calyx_library.resolve())
            hardware_hash = digest(compiled.output)
            tb = root / 'tb.sv'
            tb.write_text(TB)
            build = root / 'verilator'
            run([str(args.verilator), '--binary', '--timing', '--assert', '-Wno-fatal',
                 '--top-module', 'tb', '-Mdir', str(build), '-o', 'simulator',
                 str(tb), str(compiled.output)], root, 'build')
            stdout = run([str(build/'simulator')], root, 'simulation')
            results = [line.split()[1] for line in stdout.splitlines() if line.startswith('RESULT ')]
            if len(results) != 1 or int(results[0], 16) != words[address] % (1 << 64):
                raise ValueError('narrow ROM read differs from original 64-bit word')
            if digest(compiled.output) != hardware_hash:
                raise ValueError('hardware changed during verification')
            synthesis = bind_initialized_memories(ir, manifest, primitive,
                root / 'synthesis', tools, calyx_library=args.calyx_library.resolve(), synthesis=True)
            if any(c in str(synthesis.output) for c in ('"', '\\', '\n')):
                raise ValueError('unsupported Yosys input path')
            run([str(args.yosys), '-Q', '-T', '-p',
                 f'read_verilog -sv -D SYNTHESIS "{synthesis.output}"; hierarchy -top main; proc; memory_collect; write_json memory.json'], root, 'yosys')
            design = json.loads((root/'memory.json').read_text())
            memories = [cell for module in design['modules'].values() for cell in module['cells'].values()
                        if cell['type']=='$mem_v2']
            if len(memories)!=1 or int(memories[0]['parameters']['WIDTH'], 2)!=8:
                raise ValueError('Yosys storage is not one eight-bit memory')
            init = int(memories[0]['parameters']['INIT'], 2)
            expected_init = sum((word & 255) << (8*i) for i, word in enumerate(words))
            if int(memories[0]['parameters']['SIZE'], 2)!=3 or init!=expected_init:
                raise ValueError('Yosys initializer differs from the certified compact words')
            cases.append({'implementation': kind, 'signed': signed, 'address': address, 'word': words[address],
                'rtl_equal': True, 'storage_width': 8, 'port_width': 64,
                'sv_sha256': hardware_hash, 'image_sha256': digest(image),
                'stages': [asdict(r) for r in (*compiled.receipts, *synthesis.receipts)]})
            print('narrow ROM', kind, signed, address, 'RTL read and Yosys storage pass', flush=True)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'cases': cases,
        'verifier_sha256': digest(Path(__file__)),
        'scope': 'Eighteen native-bound ROM reads across all three memory implementations, with assertions and Yosys compact initializer checks; no model RTL or FPGA-fit acceptance.'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
