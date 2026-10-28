#!/usr/bin/env python3
"""Build an initialized block simulator without runtime vectors or answers."""
import argparse
from pathlib import Path
import sys

D=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(D),str(D/'scripts/comparison')]
from compiler.stages import StageSpec,digest,run_stage,verify_stage_receipt


def build_block_simulator(root,sv_stage,testbench,output,*,verilator,make,timeout_seconds=7200):
    output=Path(output)
    if output.is_symlink():
        raise ValueError('block build output must not be a symlink')
    root,sv_stage,testbench,output=(Path(p).resolve() for p in (root,sv_stage,testbench,output))
    if any(output==p or output.is_relative_to(p) or p.is_relative_to(output)
           for p in (root,sv_stage,testbench,Path(verilator).resolve(),Path(make).resolve())):
        raise ValueError('block build output overlaps input')
    (output/'success.json').unlink(missing_ok=True)
    deps=verify_stage_receipt(sv_stage)
    rtl=sv_stage/'kernel.sv';bindings=root/'bindings-reused.json'
    if deps.get(str(bindings))!=digest(bindings) or str(rtl) not in deps:
        raise ValueError('block initialized RTL/bindings lack compilation provenance')
    if output.exists() and any(output.iterdir()):
        raise ValueError('block simulator requires a fresh output directory')
    if timeout_seconds<=0 or testbench.is_symlink() or not testbench.is_file():
        raise ValueError('regular block testbench and positive timeout required')
    native,make=(Path(p).resolve(strict=True) for p in (verilator,make))
    native_binary=native.with_name('verilator_bin')
    if not native_binary.is_file():
        raise ValueError('native Verilator required')
    # Capture and lowering evidence remain separate from numerical acceptance.
    for path in (root/'capture/model.pt2',root/'capture/capture.json',
                 root/'capture/graph-inputs.json',root/'raw.mlir',testbench,Path(__file__).resolve()):
        if path.is_symlink() or not path.is_file():
            raise ValueError('block capture/source dependency missing')
        deps[str(path)]=digest(path)
    lowering=root/'core-reused'
    if not lowering.is_dir():
        raise ValueError('block lowering evidence missing')
    stages=[p for p in lowering.iterdir() if p.is_dir() and (p/'spec.json').is_file()]
    if not stages:
        raise ValueError('block lowering stages missing')
    for stage in stages:
        deps.update(verify_stage_receipt(stage))
    output.mkdir(parents=True,exist_ok=True)
    config={'block_root':str(root),'initialized_sv':str(rtl),
            'testbench':str(testbench),'scope':'Initialized block simulator construction only; no numerical acceptance'}
    argv=(str(native),'--cc','--exe','--main','--timing','--assert','-Wno-fatal',
          '--top-module','tb','--Mdir','verilator','-o','simulator','-j','1',
          '--output-groups','64','--output-split','1000','--output-split-cfuncs','200',
          '-CFLAGS','-Os',str(testbench),str(rtl))
    tools={str(p):digest(p) for p in (native,native_binary)}
    emission=output/'emission'
    run_stage(StageSpec('graph-block-simulator-emission',argv,deps,tools,config,
        ('verilator/Vtb.mk','verilator/Vtb.h'),timeout_seconds),emission)
    generated=emission/'verilator'
    files=[p for p in generated.iterdir() if p.is_file()]
    if (any(p.is_symlink() for p in files) or (generated/'simulator').exists() or
            not any(p.suffix=='.cpp' for p in files)):
        raise ValueError('block emission must produce C++ sources before compilation')
    deps.update(verify_stage_receipt(emission))
    deps.update({str(p):digest(p) for p in files})
    receipt=run_stage(StageSpec('graph-block-simulator',
        (str(make),'-C',str(generated),'-f','Vtb.mk','-j1'),deps,{str(make):digest(make)},
        config,('emission/verilator/simulator',),timeout_seconds),output)
    try:
        verify_stage_receipt(output);verify_stage_receipt(emission);verify_stage_receipt(sv_stage)
    except Exception:
        (output/'success.json').unlink(missing_ok=True)
        raise
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('root','sv-stage','testbench','output','verilator','make'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--timeout-seconds',type=int,default=7200)
    args=parser.parse_args(argv)
    receipt=build_block_simulator(args.root,args.sv_stage,args.testbench,args.output,
        verilator=args.verilator,make=args.make,timeout_seconds=args.timeout_seconds)
    print(receipt.identity_sha256)


if __name__=='__main__':
    main()
