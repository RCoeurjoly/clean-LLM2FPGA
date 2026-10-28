#!/usr/bin/env python3
"""Exercise exact reduction fixtures through the declared native profile and RTL.

Vectors/expected values are supplied only after compilation. Fixture acceptance
is separate from whole-model numerical acceptance and physical implementation.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from compiler.pipeline import CompilerTool, integer_profile, export_memory_inventory
from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt


def sqrt_fixture(steps):
    lines = ['module { func.func @main(%in: memref<2xi64>, %out: memref<1xi64>) {',
             '%i0 = arith.constant 0 : index', '%i1 = arith.constant 1 : index',
             '%one = arith.constant 1 : i64',
             '%input = memref.load %in[%i0] : memref<2xi64>',
             '%initial = memref.load %in[%i1] : memref<2xi64>']
    remainder, result = '%input', '%initial'
    for i in range(steps):
        lines += [f'%bit{i} = arith.constant {1 << (2*(steps-1-i))} : i64',
                  f'%candidate{i} = arith.addi {result}, %bit{i} : i64',
                  f'%take{i} = arith.cmpi sge, {remainder}, %candidate{i} : i64',
                  f'%sub{i} = arith.subi {remainder}, %candidate{i} : i64',
                  f'%half{i} = arith.shrsi {result}, %one : i64',
                  f'%plus{i} = arith.addi %half{i}, %bit{i} : i64',
                  f'%r{i} = arith.select %take{i}, %sub{i}, {remainder} : i64',
                  f'%s{i} = arith.select %take{i}, %plus{i}, %half{i} : i64']
        remainder, result = f'%r{i}', f'%s{i}'
    lines += [f'memref.store {result}, %out[%i0] : memref<1xi64>', 'return } }']
    return '\n'.join(lines)


POINTWISE = '''module { func.func @main(%in: memref<6xi64>, %out: memref<6xi64>) {
 %one = arith.constant 1 : i64
 %seven = arith.constant 7 : i64
 %a = memref.alloc() : memref<6xi64>
 %b = memref.alloc() : memref<6xi64>
 memref.copy %in, %a : memref<6xi64> to memref<6xi64>
 linalg.generic {indexing_maps=[affine_map<(i)->(i)>,affine_map<(i)->(i)>],iterator_types=["parallel"]}
 ins(%a:memref<6xi64>) outs(%b:memref<6xi64>) {
 ^bb0(%x:i64,%unused:i64):
  %v = arith.addi %x, %one : i64
  linalg.yield %v : i64
 }
 linalg.generic {indexing_maps=[affine_map<(i)->(i)>,affine_map<(i)->(i)>],iterator_types=["parallel"]}
 ins(%b:memref<6xi64>) outs(%a:memref<6xi64>) {
 ^bb0(%x:i64,%unused:i64):
  %v = arith.muli %x, %seven : i64
  linalg.yield %v : i64
 }
 memref.copy %a, %out : memref<6xi64> to memref<6xi64>
 return } }'''


def signed(value):
    return ((value + (1 << 63)) % (1 << 64)) - (1 << 63)


def sqrt_reference(value, initial, steps):
    if initial == 0:
        return min(math.isqrt(max(0, value)), (1 << steps)-1)
    remainder, result = value, initial
    for bit in (1 << exponent for exponent in range(2*(steps-1), -1, -2)):
        candidate = signed(result+bit)
        take = remainder >= candidate
        remainder = signed(remainder-candidate) if take else remainder
        result = signed((result >> 1)+bit) if take else result >> 1
    return result


def run(command, directory, label, timeout=180):
    result = subprocess.run(command, cwd=directory, text=True, capture_output=True, timeout=timeout)
    (directory/(label+'.stdout.log')).write_text(result.stdout)
    (directory/(label+'.stderr.log')).write_text(result.stderr)
    if result.returncode:
        raise ValueError(f'{label}: {result.returncode}: {result.stderr[-3000:]}')
    return result.stdout


def check(args):
    args.receipt.unlink(missing_ok=True)
    tools = {name: CompilerTool(getattr(args, name).resolve(strict=True))
             for name in ('torch', 'memory', 'translate', 'calyx')}
    dependencies = {str(Path(__file__)): digest(Path(__file__)),
                    str(args.verilator): digest(args.verilator),
                    str(ROOT/'compiler/stages.py'): digest(ROOT/'compiler/stages.py'),
                    str(ROOT/'compiler/pipeline.py'): digest(ROOT/'compiler/pipeline.py')}
    results = []
    for name, source, output_depth in (('sqrt32', sqrt_fixture(32), 1),
                                      ('sqrt29', sqrt_fixture(29), 1),
                                      ('pointwise', POINTWISE, 6)):
        for selected in (False, True):
            directory = args.work_dir.resolve()/(name+('-selected' if selected else '-baseline'))
            directory.mkdir(parents=True, exist_ok=True)
            current = directory/'input.mlir'
            current.write_text(source)
            profile = integer_profile(recover_restoring_sqrt_loops=selected,
                                      coalesce_pointwise_pingpong=selected)
            for stage, role, arguments, filename in profile[2:]:
                declared_inputs = {**dependencies, str(current): digest(current)}
                if stage == 'sv':
                    arguments += ('-l', str(args.calyx_library))
                    declared_inputs.update({str(p): digest(p) for p in args.calyx_library.rglob('*') if p.is_file()})
                tool = tools[role]
                target = directory/stage
                run_stage(StageSpec(stage, (str(tool.executable), str(current), *arguments, '-o', filename),
                    declared_inputs, tool.identities(), {'exact_reductions': str(selected)},
                    (filename,), 180), target)
                dependencies.update(verify_stage_receipt(target))
                current = target/filename
            sv = current
            inventory, _ = export_memory_inventory(directory/'memory-contracts/calyx.mlir',
                                                  directory/'inventory', tools['memory'])
            dependencies.update(verify_stage_receipt(inventory.parent))
            memories = [m for m in json.loads(inventory.read_text())['memories'] if m['physical']]
            if name.startswith('sqrt'):
                loops = (directory/'loops/loops.mlir').read_text()
                if loops.count('llm2fpga.restoring_sqrt_tail') != int(selected):
                    raise ValueError('sqrt selection did not recover exactly the expected tail')
            else:
                scratch = (directory/'scratch/buffers.mlir').read_text()
                if scratch.count('memref.alloc()') != (1 if selected else 2):
                    raise ValueError('pointwise selection did not remove exactly one allocation')
            tb = directory/'tb.sv'
            tb.write_text(f'''module tb;
reg clk=0; always #5 clk=~clk;
reg reset=1; reg go=0; wire done;
integer cycles=0;
main dut(.clk(clk),.reset(reset),.go(go),.done(done));
always @(posedge clk) if(go && !done) cycles <= cycles+1;
initial begin
 repeat(4) @(negedge clk); reset=0; go=1;
 wait(done===1'b1); @(negedge clk);
 for(integer i=0;i<{output_depth};i=i+1) $display("RESULT %h",dut.mem_1.mem[i]);
 $display("CYCLES %d",cycles); $finish;
end
initial begin repeat(100000) @(posedge clk); $fatal(1,"timeout"); end
endmodule\n''')
            build = directory/'verilator'
            run([str(args.verilator), '--binary', '--timing', '--assert', '-Wno-fatal',
                 '--top-module', 'tb', '-Mdir', str(build), '-o', 'simulator',
                 str(tb), str(sv)], directory, 'build')
            # Runtime vectors and reference results are deliberately constructed
            # only after all compiler stages and the simulator build finish.
            if name.startswith('sqrt'):
                steps = 32 if name == 'sqrt32' else 29
                cases = [([v, 0], [sqrt_reference(v, 0, steps)])
                         for v in (-(1 << 63), -1, 0, 1, 2, 3, 4, 15, 16, 17, (1 << 63)-1)]
                cases += [([v, initial], [sqrt_reference(v, initial, steps)]) for v, initial in
                          ((-(1 << 63), (1 << 63)-1), (-7, -1), ((1 << 63)-1, -(1 << 63)))]
            else:
                values = [-(1 << 63), -1, 0, 1, (1 << 63)-2, (1 << 63)-1]
                cases = [(values, [signed((v+1)*7) for v in values])]
            comparisons = []
            for i, (inputs, expected) in enumerate(cases):
                data = directory/f'data-{i}'
                data.mkdir(exist_ok=True)
                for m in memories:
                    values = inputs if m['cell'] == 'mem_0' else [0]*m['depth']
                    if m['width'] != 64 or len(values) != m['depth']:
                        raise ValueError('fixture ABI/geometry changed')
                    (data/(m['cell']+'.dat')).write_text(''.join(f'{v % (1 << 64):016x}\n' for v in values))
                output = run([str(build/'simulator'), '+DATA='+str(data)], data, 'simulate', 30)
                actual = [signed(int(line.split()[1], 16)) for line in output.splitlines() if line.startswith('RESULT ')]
                if actual != expected:
                    raise ValueError(f'{name}/{selected}/{i}: {actual} != {expected}')
                cycles = [int(line.split()[1]) for line in output.splitlines() if line.startswith('CYCLES ')]
                if len(cycles) != 1 or cycles[0] <= 0:
                    raise ValueError('missing cycle observation')
                comparisons.append({'input': inputs, 'actual': actual, 'expected': expected, 'cycles': cycles[0]})
            dependencies[str(tb)] = digest(tb)
            dependencies[str(build/'simulator')] = digest(build/'simulator')
            results.append({'kernel': name, 'selected': selected, 'memory_words': sum(m['depth'] for m in memories),
                            'cases': comparisons, 'sv_sha256': digest(sv)})
    for name, identity in dependencies.items():
        if digest(Path(name)) != identity:
            raise ValueError('changed dependency: '+name)
    args.receipt.write_text(json.dumps({'schema_version': 1, 'results': results,
        'dependency_hashes': dependencies, 'scope': 'Focused native kernel RTL, not full-model acceptance'}, indent=2)+'\n')
    print(sum(len(r['cases']) for r in results), 'exact-reduction RTL comparisons PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('torch', 'memory', 'translate', 'calyx', 'verilator', 'calyx-library', 'work-dir', 'receipt'):
        parser.add_argument('--'+name, type=Path, required=True)
    check(parser.parse_args())
