#!/usr/bin/env python3
"""Fresh native-Lean/Verilator whole-design reset comparison, without stage skipping."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
work = Path('/tmp/llm2fpga-full-hardware-20261102')
rtl = Path('/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl')
lean = '/tmp/llm2fpga-tools/lean-4.19.0-linux/bin/lean'
verilator = '/nix/store/i5hjqqy2qk6jac8mnnaxnlh8yfi49wjh-verilator-5.052/bin/verilator'
work.mkdir(parents=True, exist_ok=True)
shutil.copyfile(root / 'FullReset.lean', work / 'FullReset.lean')
env = os.environ.copy()
env['LEAN_PATH'] = str(work)
subprocess.run([sys.executable, str(root / 'format_sv_lines.py')], check=True, env=env)
stages = [
    ('10-native-runtime-c', [lean, '--root=' + str(work), '-c', str(work / 'Functional.c'),
                            str(work / 'Chisel2Lean/Functional.lean')]),
    ('11-native-trace-c', [lean, '--root=' + str(work), '-c', str(work / 'FullReset.c'), str(work / 'FullReset.lean')]),
    ('12-native-link', [str(Path(lean).with_name('leanc')), '-O3', '-o', str(work / 'full-reset-lean'),
                       str(work / 'Functional.c'), str(work / 'FullReset.c')]),
    ('13-verilator-build', [verilator, '--binary', '--timing', '-j', '2', '--output-split', '10000',
                           '--output-split-cfuncs', '2000', '-Wno-fatal', '--top-module', 'full_reset_tb',
                           '--Mdir', str(work / 'verilator'), '-o', 'full-reset-sim',
                           str(root / 'full-reset-tb.sv'), str(rtl / 'core.sv'),
                           str(work / 'model-formatted.sv'), str(rtl / 'memory.sv')]),
    ('14-verilator-trace', [str(work / 'verilator/full-reset-sim')]),
    ('15-native-full-trace', [str(work / 'full-reset-lean'), str(work / 'TinyStoriesFull.circuit.bin')]),
]
for name, command in stages:
    subprocess.run([sys.executable, str(root / 'run_stage.py'), name, *command], check=True, env=env)
subprocess.run([sys.executable, str(root / 'compare_reset.py'), '--rtl-log', str(root / '14-verilator-trace.log'),
                '--lean-log', str(root / '15-native-full-trace.log'), '--simulator', 'Verilator 5.052, two-state',
                '--report', str(root / 'verilator-reset-result.json')], check=True, env=env)
