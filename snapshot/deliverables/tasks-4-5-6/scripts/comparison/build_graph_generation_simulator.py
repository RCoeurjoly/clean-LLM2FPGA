#!/usr/bin/env python3
"""Verification-only simulator build with non-overlapping native/C++ stages."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

D = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(D), str(D/'scripts/comparison')]
from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt
from graph_rtl_runtime import emit_generation_testbench


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('rtl', 'output', 'verilator', 'make'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--logit-words', type=int, default=50257)
    parser.add_argument('--timeout', type=int, default=7200)
    parser.add_argument('--emit', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    rtl, output = args.rtl.resolve(strict=True), args.output.resolve()
    native, make = args.verilator.resolve(strict=True), args.make.resolve(strict=True)
    source = Path(__file__).resolve()
    tb = output/'tb.sv'
    generated = output/'verilator'
    if args.emit:
        subprocess.run([str(native), '--cc', '--exe', '--main', '--timing', '--assert',
            '--top-module', 'tb', '-Wno-fatal', '--Mdir', str(generated),
            '-o', 'simulator', '-j', '1', '--output-groups', '64',
            '--output-split', '1000', '--output-split-cfuncs', '200',
            '-CFLAGS', '-Os', str(tb), str(rtl)], check=True)
        if (generated/'simulator').exists():
            raise ValueError('native emission unexpectedly compiled simulator')
        files = sorted(path for path in generated.iterdir() if path.is_file())
        if not (generated/'Vtb.mk').is_file() or not any(p.suffix == '.cpp' for p in files):
            raise ValueError('native emission produced no simulator sources')
        if any(p.is_symlink() for p in files):
            raise ValueError('symlink in emitted simulator sources')
        manifest = {str(path): digest(path) for path in files}
        Path('generated-sources.json').write_text(json.dumps(manifest, indent=2)+'\n')
        return
    if output.exists() and any(output.iterdir()):
        raise ValueError('simulator build requires a fresh output directory')
    output.mkdir(parents=True, exist_ok=True)
    tb.write_text(emit_generation_testbench(args.logit_words))
    inputs = {str(path): digest(path) for path in (rtl, tb, source)}
    tools = {str(path): digest(path) for path in (Path(sys.executable).resolve(), native)}
    native_binary = native.with_name('verilator_bin')
    if native_binary.is_file():
        tools[str(native_binary)] = digest(native_binary)
    argv = (sys.executable, str(source), '--emit', '--rtl', str(rtl),
            '--output', str(output), '--verilator', str(native), '--make', str(make))
    # sys.executable can itself be a symlink; StageSpec uses argv[0] identity.
    tools[sys.executable] = digest(Path(sys.executable))
    emission = output/'emission'
    run_stage(StageSpec('graph-generation-simulator-emission', argv,
        inputs, tools, {}, ('generated-sources.json',), args.timeout), emission)
    dependencies = verify_stage_receipt(emission)
    sources = json.loads((emission/'generated-sources.json').read_text())
    for name, identity in sources.items():
        path = Path(name)
        if not path.is_relative_to(generated) or path.is_symlink() or digest(path) != identity:
            raise ValueError('emitted simulator source changed')
    dependencies.update(inputs)
    dependencies.update(sources)
    run_stage(StageSpec('graph-generation-simulator',
        (str(make), '-C', 'verilator', '-f', 'Vtb.mk', '-j1'),
        dependencies, {str(make): digest(make)}, {},
        ('verilator/simulator',), args.timeout), output)
    verify_stage_receipt(emission)


if __name__ == '__main__':
    main()
