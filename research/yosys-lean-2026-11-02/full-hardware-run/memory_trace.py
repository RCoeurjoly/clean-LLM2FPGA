#!/usr/bin/env python3
"""Compare a defined reset/write/read trace of production memory RTL and Lean."""

import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
source = root.parent / 'model-probe/memory-results'
upstream = root.parent / 'netlist-formats/evidence/ErosSph__xiangshan2Lean'
sys.path.insert(0, str(upstream / 'scripts'))
from functional_ir import import_functional_file
from lean_table import emit_table

work = Path('/tmp/llm2fpga-lean-memory-table')
work.mkdir(parents=True, exist_ok=True)
module = json.loads((source / 'lowered.json').read_text())['modules']['model_memory']
ir = import_functional_file(source / 'functional.smt2', module, 'TinyStoriesMemory', None,
                            compact_nodes=True)
emit_table(ir, work, runtime_source=(upstream / 'lean/Chisel2Lean/Functional.lean').read_bytes())
env = os.environ.copy()
env['LEAN_PATH'] = str(work)
lean = '/tmp/llm2fpga-tools/lean-4.19.0-linux/bin/lean'
setup_commands = []
for name in ['Chisel2Lean/Functional', 'TinyStoriesMemory']:
    command = [lean, '--root=' + str(work), '-o', str(work / (name + '.olean')),
               str(work / (name + '.lean'))]
    compiled = subprocess.run(command, text=True, capture_output=True, env=env)
    (root / ('memory-setup-' + name.replace('/', '-') + '.log')).write_text(compiled.stdout + compiled.stderr)
    if compiled.returncode:
        raise SystemExit('Memory control compilation failed: ' + name)
    setup_commands.append(command)
initial = {field['name']: ([word['value'] for word in field['initial']['words']]
            if field['sort']['kind'] == 'memory' else field['initial']['value'])
           for field in ir['states']}
transactions = [(1,0,0,0,0), (1,0,0,0,0),
    (0,1,1,3,0x12345678), (0,1,0,3,0),
    (0,1,1,4,0x9abcdef0), (0,1,0,3,0), (0,1,0,4,0),
    (0,0,0,3,0), (1,1,1,3,0xffffffff), (0,1,0,3,0)]
trace = []
for reset, enable, write, address, data in transactions:
    for clock in [0, 1]:
        values = {field['name']: 0 for field in ir['inputs']}
        values.update(clk=clock, reset=reset, content_en=enable, write_en=write,
                      addr0=address, write_data=data)
        trace.append({'inputs': values})
request = work / 'request.json'
request.write_text(json.dumps({'initial': initial, 'trace': trace}))
run = work / 'RunMemory.lean'
run.write_text('''import TinyStoriesMemory
def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "Supply request")
  let request ← Chisel2Lean.Functional.unwrap (Lean.Json.parse (← IO.FS.readFile path))
  let checked ← Chisel2Lean.Functional.unwrap TinyStoriesMemory.circuit
  Chisel2Lean.Functional.runRequest checked request
''')
command = [lean, '--root=' + str(work),
           '--run', str(run), str(request)]
lean_run = subprocess.run(command, text=True, capture_output=True, env=env)
(root / 'memory-lean-trace.log').write_text(lean_run.stdout + lean_run.stderr)
if lean_run.returncode:
    raise SystemExit('Lean trace failed; see memory-lean-trace.log')
lean_rows = [json.loads(line) for line in lean_run.stdout.splitlines() if line.startswith('{')]
tb = ['module tb;', 'reg clk=0,reset=1,content_en=0,write_en=0;',
      'reg [3:0] addr0=0; reg [31:0] write_data=0;',
      'wire [31:0] read_data; wire done; model_memory dut(.*);', 'initial begin']
for index, row in enumerate(trace):
    values = row['inputs']
    tb += [' '.join(f'{key}={values[key]};' for key in
                   ['reset','content_en','write_en','addr0','write_data','clk']), '#1;',
           f'$display("%0d %h %b",{index},read_data,done);']
tb += ['$finish;', 'end', 'endmodule']
(work / 'tb.sv').write_text('\n'.join(tb) + '\n')
icarus = '/nix/store/smablxy777s8dd47vcm36qzhl1npjw7m-iverilog-12.0/bin'
compile_command = [icarus + '/iverilog', '-g2012', '-s', 'tb', '-o', str(work / 'sim'),
                   str(work / 'tb.sv'), str(source / 'model_memory.sv')]
subprocess.run(compile_command, check=True, capture_output=True)
rtl_run = subprocess.run([icarus + '/vvp', str(work / 'sim')], check=True, text=True,
                         capture_output=True)
(root / 'memory-rtl-trace.log').write_text(rtl_run.stdout)
rtl_rows = []
for line in rtl_run.stdout.splitlines():
    fields = line.split()
    if len(fields) == 3 and fields[0].isdigit():
        rtl_rows.append((int(fields[0]), fields[1], fields[2]))
assert len(lean_rows) == len(rtl_rows) == len(trace)
comparisons = 0
for index, read, done in rtl_rows:
    if index < 3:
        continue  # RTL initial FF values are X; compare after synchronous reset.
    expected = {'read_data': int(read, 16), 'done': int(done, 2)}
    assert lean_rows[index]['post'] == expected, (index, lean_rows[index], expected)
    comparisons += len(expected)
result = {'status': 'completed', 'trace_samples': len(trace),
          'compared_samples_after_reset': len(trace) - 3, 'output_comparisons': comparisons,
          'lean_setup_commands': setup_commands, 'lean_command': command,
          'iverilog_command': compile_command,
          'scope': 'One concrete legal reset/write/read/hold trace; not all-input equivalence'}
(root / 'memory-trace-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
