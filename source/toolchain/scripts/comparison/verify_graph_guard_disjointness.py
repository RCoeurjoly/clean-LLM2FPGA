#!/usr/bin/env python3
"""Compare native guard assertions under four-state runtime inputs.

Verification only. Compiler inputs contain authored mux computation and no
expected answers. Stimuli are created after both native RTLs compile; read-only
fixture inspection selects positions in distinct bounded assertion chunks.
"""
from pathlib import Path
import argparse, hashlib, itertools, json, re, subprocess
parser = argparse.ArgumentParser()
for name in ('before', 'after', 'iverilog', 'vvp', 'output'):
    parser.add_argument('--' + name, type=Path, required=True)
args = parser.parse_args()
args.output.unlink(missing_ok=True)
root = args.output.parent / 'four-state-guard-check'
root.mkdir(exist_ok=True, parents=True)
ports = ', '.join(f'g{i}: 1' for i in range(260))
assignments = '\n'.join(f"chosen.in = g{i} & go ? 32'd{i};" for i in range(260))
source = root / 'guards.futil'
source.write_text('import "primitives/core.futil";\n' +
    f'component main({ports}) -> (result: 32) {{\ncells {{ chosen = std_wire(32); }}\nwires {{\n' +
    assignments + "\nresult = chosen.out; done = 1'd1;\n}\ncontrol {}\n}\n")
connections = ', '.join(f'.g{i}(guards[{i}])' for i in range(260))
tb = root / 'tb.sv'
tb.write_text('module tb;\nreg clk=0, reset=0, go=0;\nreg [259:0] guards=0;\nwire [31:0] result; wire done;\n' +
    f'main dut(.clk(clk),.reset(reset),.go(go),.result(result),.done(done),{connections});\n' +
    'initial begin\nif (!$value$plusargs("GUARDS=%b",guards)) $fatal(1,"missing runtime guards");\n' +
    '#1; $display("EXPECTED %0d",$onehot0(guards)); go=1;\n#1; clk=1;\n#1; $display("CHECK_PASS"); $finish;\nend\nendmodule\n')
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
compiled = {}
for label, backend in [('before', args.before), ('after', args.after)]:
    directory = root / label
    directory.mkdir(exist_ok=True)
    rtl = directory / 'guards.sv'
    subprocess.run([str(backend), str(source), '-b', 'verilog', '-l',
                    str(backend.parent.parent / 'share/calyx'), '-o', str(rtl)],
                   check=True, capture_output=True, text=True, timeout=60)
    binary = directory / 'simulator.vvp'
    result = subprocess.run([str(args.iverilog), '-g2012', '-s', 'tb', '-o', str(binary),
                             str(tb), str(rtl)], capture_output=True, text=True, timeout=60)
    (directory / 'build.log').write_text(result.stdout + result.stderr)
    if result.returncode: raise RuntimeError(label + ': ' + result.stderr)
    compiled[label] = {'binary': binary, 'rtl_sha256': sha(rtl), 'binary_sha256': sha(binary)}
# Read-only fixture inspection selects runtime positions in each actual chunk.
# No compiler input, emitted RTL, or simulator executable is rewritten.
rtl_text = (root / 'after' / 'guards.sv').read_text()
bindings = dict(re.findall(r'^wire (_guard\d+) = ([^;]+);', rtl_text, re.MULTILINE))
def guard_input(name):
    expression = bindings[name]
    found = set(int(x[1:]) for x in re.findall(r'\bg\d+\b', expression))
    for dependency in re.findall(r'\b_guard\d+\b', expression):
        found.update(guard_input(dependency))
    return found
chunks = []
for match in re.finditer(r'\$onehot0\(\{([^{}]+)\}\)', rtl_text):
    names = re.findall(r'\b_guard\d+\b', match.group(1))
    if not names: continue
    ports = []
    for name in names:
        resolved = guard_input(name)
        if len(resolved) != 1: raise ValueError('fixture guard origin is ambiguous')
        ports.append(next(iter(resolved)))
    chunks.append(ports)
if list(map(len, chunks)) != [128,128,4]: raise ValueError('fixture chunk domain changed')
positions = [chunks[0][0],chunks[0][-1],chunks[1][0],chunks[2][0]]
if len(set(positions)) != 4: raise ValueError('runtime guard positions overlap')
records = []
# Stimulus and expected classifications are created only after both RTLs compile.
for index, pattern in enumerate(itertools.product('01xz', repeat=4)):
    bits = ['0'] * 260
    for position, bit in zip(positions, pattern, strict=True): bits[position] = bit
    stimulus = ''.join(reversed(bits))
    expected = pattern.count('1') <= 1
    record = {'pattern': ''.join(pattern), 'expected_disjoint': expected}
    for label in ('before', 'after'):
        result = subprocess.run([str(args.vvp), str(compiled[label]['binary']), '+GUARDS=' + stimulus],
                                capture_output=True, text=True, timeout=5)
        if f'EXPECTED {int(expected)}' not in result.stdout:
            raise ValueError(f'{label} {pattern}: builtin oracle differs: {result.stdout}')
        if expected:
            if result.returncode or 'CHECK_PASS' not in result.stdout:
                raise ValueError(f'{label} {pattern}: valid/unknown guards rejected: {result.stdout}')
        elif not result.returncode or 'Multiple assignment to port' not in result.stdout:
            raise ValueError(f'{label} {pattern}: overlapping guards accepted: {result.stdout}')
        record[label] = {'exit_code': result.returncode, 'stdout_sha256':
                        hashlib.sha256(result.stdout.encode()).hexdigest()}
    records.append(record)
    if (index + 1) % 64 == 0: print(index + 1, 'four-state patterns agree', flush=True)
receipt = {'schema_version': 1,
    'scope': '256 exhaustive 0/1/X/Z assignments at four positions spanning three bounded chunks (260 guards). Checks the original builtin and both native backend assertions; not a whole-model formal proof.',
    'fixture_sha256': sha(source), 'testbench_sha256': sha(tb),
    'verifier_sha256': sha(Path(__file__)), 'positions': positions, 'chunk_sizes': list(map(len,chunks)),
    'tool_sha256': {name:sha(getattr(args,name)) for name in ['before','after','iverilog','vvp']},
    'compiled': {key:{k:v for k,v in value.items() if k!='binary'} for key,value in compiled.items()},
    'cases': records}
args.output.write_text(json.dumps(receipt, indent=2)+'\n')
