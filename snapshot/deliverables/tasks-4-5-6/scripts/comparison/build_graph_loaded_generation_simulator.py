#!/usr/bin/env python3
"""Build the authenticated full-core loader/bank/CDC numerical simulator.

Compilation receives no runtime prompts or expected answers. The native-memory
peer does not establish controller, PHY, calibration, timing or board acceptance.
"""
import argparse
import json
from pathlib import Path
import re
import sys

D=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(D),str(D/'scripts/comparison')]
from compiler.build_core import verify_core_receipt
from compiler.ddr_transport import verify_ddr_transport_receipt
from compiler.ddr_bank_adapter import emit_ddr_bank_adapter_stage
from compiler.stages import StageSpec,digest,run_stage,verify_stage_receipt
from graph_rtl_runtime import emit_loaded_generation_testbench


def _prepare_output(core,transport,output,protected=()):
    output=Path(output)
    if output.is_symlink():
        raise ValueError('simulator output must not be a symlink')
    output=output.resolve()
    sources=(core,transport,*protected,Path(__file__),
             D/'scripts/comparison/graph_rtl_runtime.py',
             D/'scripts/comparison/graph_generation_progress_main.cpp')
    if any(output==p or output.is_relative_to(p) or p.is_relative_to(output)
           for p in (Path(name).resolve() for name in sources)):
        raise ValueError('simulator output overlaps core/transport/tool inputs')
    (output/'success.json').unlink(missing_ok=True)
    return output


def build_loaded_generation_simulator(core,profile,transport,output,*,
                                      verilator,make,timeout_seconds=7200):
    """Consume a common graph core and its checked native transport export."""
    # Invalidate only this builder's acceptance, before input authentication.
    output=_prepare_output(core,transport,output,
        (profile.numerical_contract,profile.calyx_library,verilator,make,
         *(tool.executable for tool in profile.tools.values())))
    core,transport=(Path(p).resolve() for p in (core,transport))
    profile.validate()
    if not profile.offchip_immutable:
        raise ValueError('loaded simulator requires an off-chip profile')
    if timeout_seconds<=0:
        raise ValueError('positive simulator stage timeout required')
    if output.exists() and any(output.iterdir()):
        raise ValueError('simulator build requires a fresh output directory')
    compiled=verify_core_receipt(core,profile)
    joined=verify_ddr_transport_receipt(transport,profile)
    if Path(joined['core']).resolve()!=core or joined['core_identity']!=compiled['identity_sha256']:
        raise ValueError('transport does not belong to the selected core')
    deps={**compiled['dependency_hashes'],**joined['dependency_hashes']}
    for path in (core/'core.json',transport/'transport.json'):
        deps[str(path)]=digest(path)
    interface=core/'initialized/external-interface/interface.json'
    layout_path=core/'ddr/layout.json'
    for path in (interface,layout_path):
        if deps.get(str(path))!=digest(path):
            raise ValueError('simulator interface/layout lacks core provenance')
    layout=json.loads(layout_path.read_text())
    if layout['data_width']!=576 or layout['address_width']!=25:
        raise ValueError('unsupported loaded simulator native-memory geometry')
    roles={m['runtime_port']:m for m in json.loads(interface.read_text())['memories']
           if m.get('runtime_port')}
    if (set(roles)!={'prompt','generated_tokens','last_logits'} or
            roles['prompt']['depth']!=4 or roles['generated_tokens']['depth']!=16 or
            roles['last_logits']['depth']!=50257 or
            any(m['width']!=64 or m['read_latency']!=2 for m in roles.values())):
        raise ValueError('unsupported full-generation runtime memory contract')
    components=transport/'native-core/components'
    manifest_path=components/'manifest.json'
    manifest=json.loads(manifest_path.read_text())
    if manifest['schema_version']!=1 or manifest['entrypoint']!='main':
        raise ValueError('invalid full-core component manifest')
    rtl=[transport/'native-core/library.sv']
    for row in manifest['components']:
        if not re.fullmatch(r'component-[0-9]+\.sv',row['file']):
            raise ValueError('invalid native component filename')
        rtl.append(components/row['file'])
    # Use the compilation source origins, including immutable Nix sources;
    # a local file with the same basename cannot replace a declared primitive.
    origins=profile.document()['dependency_hashes']
    for name in ('closure_ddr_read_port.sv','closure_ddr_read_bank.sv','closure_ddr_read_cdc.sv',
                 'closure_ddr_image_loader.sv','closure_ddr_loaded_port.sv'):
        matches=[Path(path) for path in origins
                 if Path(path).parts[-3:]==('rtl','primitives',name)]
        if len(matches)!=1:
            raise ValueError('ambiguous or missing compiled primitive source: '+name)
        rtl.append(matches[0])
    for path in rtl:
        if path.is_symlink() or deps.get(str(path))!=digest(path):
            raise ValueError('simulator RTL lacks core/transport provenance: '+str(path))
    native,make=(Path(p).resolve(strict=True) for p in (verilator,make))
    native_binary=native.with_name('verilator_bin')
    if not native_binary.is_file():
        raise ValueError('native Verilator executable required')
    observer=D/'scripts/comparison/graph_generation_progress_main.cpp'
    runtime=D/'scripts/comparison/graph_rtl_runtime.py'
    for path in (Path(__file__).resolve(),observer,runtime,manifest_path):
        deps[str(path)]=digest(path)
    output.mkdir(parents=True,exist_ok=True)
    # Numerical execution uses a behavioral native-memory peer. The physical
    # transport has already joined its controller and therefore exposes a
    # different boundary. Rebuild only the adapter, through typed HW IR, from
    # the authenticated interface/layout; never edit either generated design.
    adapter=output/'adapter'
    emit_ddr_bank_adapter_stage(interface,layout_path,adapter,profile.tools['circt'],
        load_images=True,timeout_seconds=timeout_seconds)
    deps.update(verify_stage_receipt(adapter/'emission'))
    rtl.append(adapter/'emission/rtl/graph_ddr_bank.sv')
    tb=output/'tb.sv'
    tb.write_text(emit_loaded_generation_testbench(50257,layout['frames']))
    deps[str(tb)]=digest(tb)
    config={'core_identity':compiled['identity_sha256'],
            'transport_identity':joined['identity_sha256'],
            'scope':'Full sixteen-step loaded-bank/CDC simulator build only; '
                    'behavioral native memory, no numerical or physical acceptance'}
    argv=(str(native),'--cc','--exe','--timing','--assert','--top-module','tb',
          '-Wno-fatal','--Mdir','verilator','-o','simulator','-j','1',
          '--output-groups','64','--output-split','1000','--output-split-cfuncs','200',
          '-CFLAGS','-O3',str(observer),str(tb),*map(str,rtl))
    tools={str(path):digest(path) for path in (native,native_binary)}
    emission=output/'emission'
    run_stage(StageSpec('graph-loaded-generation-simulator-emission',argv,deps,
        tools,config,('verilator/Vtb.mk','verilator/Vtb.h'),timeout_seconds),emission)
    generated=emission/'verilator'
    files=sorted(p for p in generated.iterdir() if p.is_file())
    if ((generated/'simulator').exists() or not any(p.suffix=='.cpp' for p in files)
            or any(p.is_symlink() for p in files)):
        raise ValueError('native emission must produce regular C++ sources only')
    deps.update(verify_stage_receipt(emission))
    deps.update({str(path):digest(path) for path in files})
    result=run_stage(StageSpec('graph-loaded-generation-simulator',
        (str(make),'-C',str(generated),'-f','Vtb.mk','-j1'),deps,
        {str(make):digest(make)},config,('emission/verilator/simulator',),
        timeout_seconds),output)
    try:
        verify_stage_receipt(emission)
        verify_stage_receipt(output)
        verify_core_receipt(core,profile)
        verify_ddr_transport_receipt(transport,profile)
    except Exception:
        (output/'success.json').unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('core','transport','output','verilator','make','numerical-contract','calyx-library'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--timeout-seconds',type=int,default=7200)
    args=parser.parse_args(argv)
    _prepare_output(args.core,args.transport,args.output,
        (args.numerical_contract,args.calyx_library,args.verilator,args.make))
    from scripts.pipeline.export_graph_ddr_transport import read_profile
    profile=read_profile(args.core,args.numerical_contract,args.calyx_library)
    result=build_loaded_generation_simulator(args.core,profile,args.transport,args.output,
        verilator=args.verilator,make=args.make,timeout_seconds=args.timeout_seconds)
    print(result.identity_sha256)


if __name__=='__main__':
    main()
