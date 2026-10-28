#!/usr/bin/env python3
"""Verify memory reductions through toy PyTorch generation and scratch RTL.

Expected values and runtime vectors are created after compilation. This fixture
uses the complete sixteen-context controller and real public memory protocol;
it does not establish full TinyStories-1M numerical acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import torch
from TinyStories.serial_gemv_boundary import serial_gemv
from compiler.build_core import build_core,verify_core_receipt
from compiler.build_profile import CoreBuildProfile
from compiler.frontend.package_inputs import load_compile_inputs
from compiler.pipeline import CompilerTool,integer_profile,export_memory_inventory
from compiler.stages import StageSpec,digest,run_stage,verify_stage_receipt
from graph_rtl_runtime import emit_generation_testbench,decode_output,install_frozen_images

# Valid tokens form 0->1->2->3->0; x=0 has a winning tie at indices1/2.
# This forces changing feedback through all16 contexts and exercises negative
# fractional truncation in the constant-zero channel.
class Projection(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('weights',torch.tensor([[123,0,-78],[4,0,-4],[16,0,-4],[54,0,-27]],dtype=torch.int64))
        self.register_buffer('scales',torch.tensor([1,1,1,1],dtype=torch.int64))
    def forward(self,tokens):
        x=tokens.reshape(-1)
        lhs=torch.stack((x,-x,x+1),dim=-1)
        value=serial_gemv(lhs,self.weights)*self.scales
        return (torch.div(value,3,rounding_mode='trunc')+1).unsqueeze(0)

SCRATCH='''module {func.func @main(%in:memref<4xi64>,%out:memref<1xi64>){
 %zero=arith.constant 0:i64
 %one=arith.constant 1:i64
 %a17=arith.constant 17:i64
 %a29=arith.constant 29:i64
 %i0=arith.constant 0:index
 %i1=arith.constant 1:index
 %i3=arith.constant 3:index
 %a=memref.alloc():memref<4xi64>
 memref.copy %in,%a:memref<4xi64> to memref<4xi64>
 %v=memref.load %a[%i3]:memref<4xi64>
 %b=memref.alloc():memref<2x4xi64>
 linalg.fill ins(%zero:i64) outs(%b:memref<2x4xi64>)
 memref.store %a17,%b[%i0,%i1]:memref<2x4xi64>
 memref.store %a29,%b[%i1,%i0]:memref<2x4xi64>
 %x=memref.load %b[%i0,%i1]:memref<2x4xi64>
 %y=memref.load %b[%i1,%i0]:memref<2x4xi64>
 %sum=arith.addi %x,%y:i64
 %c=memref.alloc():memref<i64>
 linalg.fill ins(%one:i64) outs(%c:memref<i64>)
 %cv=memref.load %c[]:memref<i64>
 %mixed=arith.xori %v,%sum:i64
 %answer=arith.addi %mixed,%cv:i64
 %true=arith.constant true
 %false=arith.constant false
 %mask_a=memref.alloc():memref<4xi1>
 linalg.fill ins(%true:i1) outs(%mask_a:memref<4xi1>)
 %mask_av=memref.load %mask_a[%i0]:memref<4xi1>
 %mask_b=memref.alloc():memref<8xi1>
 linalg.fill ins(%false:i1) outs(%mask_b:memref<8xi1>)
 %mask_bv=memref.load %mask_b[%i0]:memref<8xi1>
 %mask_aw=arith.extui %mask_av:i1 to i64
 %mask_bw=arith.extui %mask_bv:i1 to i64
 %mask_zero=arith.xori %mask_aw,%one:i64
 %masked=arith.xori %answer,%mask_zero:i64
 %result=arith.xori %masked,%mask_bw:i64
 memref.store %result,%out[%i0]:memref<1xi64>
 return }}'''

def run(cmd,directory,label,timeout=180):
    result=subprocess.run(cmd,cwd=directory,text=True,capture_output=True,timeout=timeout)
    (directory/(label+'.stdout.log')).write_text(result.stdout)
    (directory/(label+'.stderr.log')).write_text(result.stderr)
    if result.returncode:raise ValueError(label+': '+result.stderr[-2000:]+result.stdout[-1000:])
    return result.stdout

def wrap(v):return ((v+(1<<63))%(1<<64))-(1<<63)

def reference(context):
    x=context[-1];lhs=(x,-x,wrap(x+1));weights=((123,0,-78),(4,0,-4),(16,0,-4),(54,0,-27));scales=(1,1,1,1)
    result=[]
    for row,scale in zip(weights,scales):
        acc=0
        for a,b in zip(lhs,row):acc=wrap(acc+wrap(a*b))
        v=wrap(acc*scale);q=abs(v)//3
        result.append(wrap((q if v>=0 else -q)+1))
    return result

def main(args):
    root=args.work_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    args.receipt.unlink(missing_ok=True)
    tools={n:CompilerTool(getattr(args,n).resolve(strict=True)) for n in ('torch','circt','memory','translate','calyx')}
    dependencies={str(Path(__file__).resolve()):digest(Path(__file__)),str(args.verilator):digest(args.verilator)}
    native_verilator=args.verilator.with_name('verilator_bin')
    if native_verilator.is_file():dependencies[str(native_verilator)]=digest(native_verilator)
    from compiler import pipeline,stages
    import graph_rtl_runtime
    for module in (pipeline,stages,graph_rtl_runtime):dependencies[str(Path(module.__file__).resolve())]=digest(Path(module.__file__))
    fixture=root/'inputs';fixture.mkdir();identities={}
    for group,files in [('snapshot',{'config.json':b'{"model_type":"verification"}','pytorch_model.bin':b'verification snapshot'}),
                        ('package',{'manifest.json':b'{"model":{}}','weights.bin':b'verification fixture','scales.bin':b'verification fixture'})]:
        directory=fixture/group;directory.mkdir()
        for name,data in files.items():(directory/name).write_bytes(data)
        identities[group]={n:hashlib.sha256(d).hexdigest() for n,d in files.items()}
    contract=fixture/'contract.json';contract.write_text(json.dumps({'schema_version':1,'inputs':identities,'arithmetic':{}}))
    inputs=load_compile_inputs(fixture/'snapshot',fixture/'package',contract)
    reports=[]
    for selected in (False,True):
        directory=root/('selected' if selected else 'baseline');directory.mkdir()
        profile=CoreBuildProfile(contract,tools,args.calyx_library,
            lut_memory_max_bits=args.lut_memory_max_bits if selected else 0,
            narrow_immutable=not(selected and args.full_width_lut_roms),
            fuse_gemv_epilogues=selected,pool_private_capacity=selected,
            stable_scratch_pooling=selected and args.stable_scratch_pooling,
            recover_restoring_sqrt_loops=selected,coalesce_pointwise_pingpong=selected)
        model=Projection()
        # Only the fixture model constructor is substituted. Capture, the common
        # graph pipeline, all16 contexts, binding and emitted SV are real.
        with patch('compiler.generation.build_fixed_point_model',return_value=model):
            build_core(inputs,profile,directory/'core',timeout_seconds=300)
        core=directory/'core';verify_core_receipt(core,profile)
        rtl=core/'initialized/sv/kernel.sv';tb=directory/'tb.sv'
        tb.write_text(emit_generation_testbench(4,max_cycles=2_000_000))
        build=directory/'verilator'
        run([str(args.verilator),'--binary','--timing','--assert','-Wno-fatal','--top-module','tb',
             '-Mdir',str(build),'-o','simulator',str(tb),str(rtl)],directory,'build',300)
        # Reference computations and test prompts begin only after compilation.
        cases=[];bindings=json.loads((core/'bindings.json').read_text())['bindings']
        for i,prompt in enumerate(([0,1,2,3],[3,2,1,0],[0,0,0,1])):
            case=directory/f'case-{i}';case.mkdir();install_frozen_images(bindings,core,case)
            (case/'prompt.hex').write_text(''.join(f'{v:016x}\n' for v in prompt))
            context=list(prompt);expected=[]
            with torch.no_grad():
                for _ in range(16):
                    logits=reference(context)
                    eager=model(torch.tensor([context],dtype=torch.int64))[0,-1].tolist()
                    if logits!=eager:raise ValueError('independent toy reference disagrees with PyTorch')
                    token=max(range(4),key=lambda j:logits[j]);expected.append(token);context.append(token)
            stdout=run([str(build/'simulator')],case,'simulate',60)
            tokens=decode_output(stdout,16,64,prefix='TOKEN');actual=[wrap(v) for v in decode_output(stdout,4,64,prefix='LOGIT')]
            if tokens!=expected or actual!=logits:raise ValueError('toy generation outputs differ')
            cases.append({'prompt':prompt,'tokens':tokens,'logits':actual})
        receipt=verify_core_receipt(core,profile);dependencies.update(receipt['dependency_hashes'])
        dependencies[str(rtl)]=digest(rtl);dependencies[str(tb)]=digest(tb);dependencies[str(build/'simulator')]=digest(build/'simulator')
        reports.append({'selected':selected,'stable_scratch_pooling':selected and args.stable_scratch_pooling,
                        'lut_memory_max_bits':profile.lut_memory_max_bits,
                        'narrow_immutable':profile.narrow_immutable,
                        'core_identity':receipt['identity_sha256'],'cases':cases})
    scratch_reports=[]
    for selected in (False,True):
        directory=root/('scratch-selected' if selected else 'scratch-baseline');directory.mkdir()
        source=directory/'input.mlir';source.write_text(SCRATCH)
        for stage,role,opts,filename in integer_profile(entrypoint='main',
                pool_private_capacity=selected,
                stable_scratch_pooling=selected and args.stable_scratch_pooling)[2:]:
            declared={str(source):digest(source),**dependencies}
            if stage=='sv':
                opts+=('-l',str(args.calyx_library))
                declared.update({str(p):digest(p) for p in args.calyx_library.rglob('*') if p.is_file()})
            tool=tools[role];out=directory/stage
            run_stage(StageSpec(stage,(str(tool.executable),str(source),*opts,'-o',filename),declared,
                tool.identities(),{'pool_private_capacity':str(selected)},(filename,),180),out)
            dependencies.update(verify_stage_receipt(out));source=out/filename
        inventory,_=export_memory_inventory(directory/'memory-contracts/calyx.mlir',directory/'inventory',tools['memory'])
        memories=[m for m in json.loads(inventory.read_text())['memories'] if m['physical']]
        tb=directory/'tb.sv';tb.write_text('''module tb; reg clk=0;always #5 clk=~clk;
reg reset=1,go=0;wire done; main dut(.clk(clk),.reset(reset),.go(go),.done(done));
initial begin repeat(4)@(negedge clk);reset=0;go=1;wait(done===1'b1);@(negedge clk);
$display("RESULT 0 %h",dut.mem_1.mem[0]);$finish;end
initial begin repeat(100000)@(posedge clk);$fatal(1,"timeout");end endmodule''')
        build=directory/'verilator'
        run([str(args.verilator),'--binary','--timing','--assert','-Wno-fatal','--top-module','tb',
             '-Mdir',str(build),'-o','simulator',str(tb),str(source)],directory,'build')
        cases=[]
        for i,values in enumerate(([1,2,3,4],[-4,-3,-2,-1],[0,0,0,-(1<<63)],[0,0,0,(1<<63)-1])):
            case=directory/f'case-{i}';case.mkdir()
            for m in memories:
                data=values if m['cell']=='mem_0' else [0]*m['depth']
                if len(data)!=m['depth'] or m['width'] not in (1,64):raise ValueError('scratch ABI changed')
                width=m['width'];digits=(width+3)//4
                (case/(m['cell']+'.dat')).write_text(''.join(f'{v%(1<<width):0{digits}x}\n' for v in data))
            actual=wrap(decode_output(run([str(build/'simulator'),'+DATA='+str(case)],case,'simulate'),1,64)[0])
            expected=wrap((values[3]^46)+1)
            if actual!=expected:raise ValueError('scratch RTL differs')
            cases.append({'input':values,'result':actual})
        scratch_reports.append({'selected':selected,'memory_words':sum(m['depth'] for m in memories),'cases':cases})
        dependencies[str(tb)]=digest(tb);dependencies[str(build/'simulator')]=digest(build/'simulator')
    for name,h in dependencies.items():
        if digest(Path(name))!=h:raise ValueError('changed dependency '+name)
    args.receipt.write_text(json.dumps({'schema_version':1,'generation':reports,'scratch':scratch_reports,
        'dependency_hashes':dependencies,'scope':'Toy full-generation and capacity fixtures only; not TinyStories numerical or physical acceptance'},indent=2)+'\n')
    print('PASS:6 full toy generations (96 tokens,24 final logits) and8 scratch RTL cases')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stable-scratch-pooling',action='store_true',
                        help='Qualify stable i1/i64 capacity pools and structural sharing')
    parser.add_argument('--lut-memory-max-bits',type=int,default=0,
                        help='Select LUT storage for small reference groups in the selected fixture')
    parser.add_argument('--full-width-lut-roms',action='store_true',
                        help='Exercise selected LUT ROMs with storage narrowing disabled')
    for name in ('torch','circt','memory','translate','calyx','verilator','calyx-library','work-dir','receipt'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    if args.lut_memory_max_bits<0:parser.error('LUT memory threshold must be nonnegative')
    if args.full_width_lut_roms and not args.lut_memory_max_bits:
        parser.error('full-width LUT ROM checks require a positive LUT memory threshold')
    main(args)
