"""Native YPCB controller elaboration, isolated from models and answer fixtures.

No generated HDL is read or rewritten. No-DM pads are supplied before Migen
elaboration. This upstream generator hook is process-local; invoke through the
CLI in a dedicated compiler stage, never concurrently in a shared interpreter.
Initialization export describes DDR commands, not verified PHY calibration.
"""
import argparse
import json
from pathlib import Path

PIN_COUNTS={'a':15,'ba':3,'dq':72,'dqs_p':9,'dqs_n':9,'clk_p':1,
            'clk_n':1,'cke':1,'cs_n':1,'odt':1}
SCALAR_PINS=('ras_n','cas_n','we_n','reset_n')


def validate_pins(pins):
    if not isinstance(pins,dict) or set(pins)!=set(PIN_COUNTS)|set(SCALAR_PINS):
        raise ValueError('invalid board pin fields')
    seen=set()
    for key in (*PIN_COUNTS,*SCALAR_PINS):
        values=pins[key]
        if key in PIN_COUNTS:
            if not isinstance(values,list) or len(values)!=PIN_COUNTS[key]:
                raise ValueError(f'invalid {key} pin count')
        else:values=[values]
        for pin in values:
            if not isinstance(pin,str) or not pin or not pin.isalnum():
                raise ValueError(f'invalid {key} pin name')
            if pin in seen:raise ValueError('duplicate board pin')
            seen.add(pin)
    return pins


def emit_controller(pins,output):
    validate_pins(pins)
    from litedram import gen,modules,phy
    from litedram.init import get_sdram_phy_init_sequence
    from litex.build.generic_platform import IOStandard,Pins,Subsignal
    from litex.build.xilinx import XilinxPlatform
    from litex.soc.integration.export import get_csr_json
    config=dict(speedgrade=-1,cpu=None,memtype='DDR3',uart='rs232',
        cmd_latency=0,sdram_module=modules.MT41K256M8,sdram_module_nb=9,
        sdram_rank_nb=1,sdram_phy=phy.A7DDRPHY,rtt_nom='60ohm',
        rtt_wr='60ohm',ron='34ohm',input_clk_freq=200e6,
        sys_clk_freq=83.333333e6,iodelay_clk_freq=200e6,cmd_buffer_depth=16,
        user_ports={'native_0':{'type':'native','block_until_ready':True}})
    def dram_ios(_config):
        subsignals=[Subsignal(key,Pins(' '.join(value) if isinstance(value,list) else value))
                    for key,value in pins.items()]
        return [('ddram',0,*subsignals,IOStandard('SSTL15'))]
    original=gen.get_dram_ios
    try:
        gen.get_dram_ios=dram_ios
        soc=gen.LiteDRAMCore(XilinxPlatform('xc7k480tffg1156-1',io=[],toolchain='openxc7'),config)
        soc.finalize()
        verilog=soc.platform.get_verilog(soc,name='closure_litedram',regular_comb=False)
    finally:gen.get_dram_ios=original
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    verilog.write(str(output/'controller.v'))
    (output/'csr.json').write_text(get_csr_json(soc=soc,csr_regions=soc.csr_regions,
        constants=soc.constants,mem_regions=soc.mem_regions))
    settings=soc.sdram.controller.settings
    sequence,mode_registers=get_sdram_phy_init_sequence(settings.phy,settings.timing)
    (output/'initialization.json').write_text(json.dumps(dict(sequence=sequence,
        mode_registers=mode_registers,scope='DDR command initialization; PHY calibration unverified'),indent=2)+'\n')
    port=soc.sdram.crossbar.masters[0]
    # LiteX's native emitter records direction on these same signal objects.
    # Its namespace supplies the emitted identifier; no HDL text is consulted.
    ports=[]
    for signal in soc.platform.constraint_manager.get_io_signals():
        direction=getattr(signal,'direction',None)
        if direction not in ('input','output','inout'):
            raise ValueError('native controller signal lacks emitted direction')
        ports.append(dict(name=verilog.ns.get_name(signal),width=len(signal),direction=direction))
    ports.sort(key=lambda record:record['name'])
    if len({record['name'] for record in ports})!=len(ports):
        raise ValueError('duplicate native controller port name')
    geometry=dict(schema_version=1,module='closure_litedram',ports=ports,
        data_width=port.data_width,address_width=port.address_width,
        sys_clk_freq=config['sys_clk_freq'],physical_data_width=72,
        initialization_required=True,calibration_verified=False,
        scope='Native controller elaboration only; no physical acceptance')
    (output/'controller.json').write_text(json.dumps(geometry,indent=2)+'\n')


def export_controller_stage(pins,output,*,python,sources,input_dependencies=None):
    """Authenticate explicit board and generator sources before native emission.

    The caller supplies the pinned SDK interpreter and installed upstream roots.
    Each upstream Python source is retained in the dependency contract. Other
    interpreter dependencies remain governed by that SDK's Nix closure.
    input_dependencies retains the verified upstream board-input stage closure;
    run_stage checks those hashes before emission and again before acceptance.
    """
    from compiler.stages import StageSpec,run_stage,digest
    pins=Path(pins).resolve(strict=True);python=Path(python).absolute()
    module=Path(__file__).resolve()
    if set(sources)!={'litedram','litex','migen'}:
        raise ValueError('declare litedram, litex and migen sources')
    dependencies={str(pins):digest(pins),str(module):digest(module)}
    for name,source in sources.items():
        source=Path(source).resolve(strict=True)
        if not (source/'__init__.py').is_file():
            raise ValueError(f'invalid {name} source root')
        for path in source.rglob('*.py'):
            dependencies[str(path.resolve())]=digest(path)
    for path,sha in (input_dependencies or {}).items():
        if path in dependencies and dependencies[path]!=sha:
            raise ValueError('conflicting controller input dependency')
        dependencies[path]=sha
    return run_stage(StageSpec('graph-litedram-export',
        (str(python),str(module),'--pins',str(pins),'--output','.',
         '--sources',json.dumps({name:str(Path(path).resolve()) for name,path in sources.items()})),
        dependencies,{str(python):digest(python)},
        {'scope':'Native controller elaboration; PHY calibration unverified',
         'sources':{name:str(Path(path).resolve()) for name,path in sources.items()}},
        ('controller.v','csr.json','initialization.json','controller.json'),120),Path(output))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pins',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--sources',help='Declared installed upstream source roots')
    args=parser.parse_args()
    if args.sources is not None:
        import importlib
        declared=json.loads(args.sources)
        if set(declared)!={'litedram','litex','migen'}:raise ValueError('invalid upstream sources')
        for name,root in declared.items():
            actual=Path(importlib.import_module(name).__file__).resolve().parent
            if actual!=Path(root).resolve():raise ValueError(f'{name} imported source identity mismatch')
    emit_controller(json.loads(args.pins.read_text()),args.output)

if __name__=='__main__':main()
