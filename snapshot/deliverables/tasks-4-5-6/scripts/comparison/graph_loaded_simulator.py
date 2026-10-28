"""Authenticate loaded simulator construction before creating reference cases.

These checks establish provenance and boundary selection, not numerical accuracy.
"""
import json
from pathlib import Path
from compiler.build_core import verify_core_receipt
from compiler.ddr_transport import verify_ddr_transport_receipt
from compiler.stages import digest,verify_stage_receipt


def validate_native_inputs(spec,required,rtl):
    if any(spec['input_hashes'].get(path)!=identity for path,identity in required.items()):
        raise ValueError('native emission lacks core/transport provenance')
    compiled={str(Path(arg).resolve()) for arg in spec['argv'] if arg.endswith('.sv')}
    if compiled!={str(Path(path).resolve()) for path in rtl}:
        raise ValueError('native command must compile all core RTL and only the declared test boundary')


def authenticate_loaded_simulator(directory,core,profile,transport):
    directory,core,transport=(Path(p).resolve() for p in (directory,core,transport))
    dependencies=verify_stage_receipt(directory)
    spec=json.loads((directory/'spec.json').read_text())
    filename='emission/verilator/simulator'
    if spec['name']!='graph-loaded-generation-simulator' or spec['outputs']!=[filename]:
        raise ValueError('expected a complete loaded simulator stage')
    emission=directory/'emission';adapter=directory/'adapter/emission'
    emitted=verify_stage_receipt(emission)
    adapted=verify_stage_receipt(adapter)
    native=json.loads((emission/'spec.json').read_text())
    bank=json.loads((adapter/'spec.json').read_text())
    if (native['name']!='graph-loaded-generation-simulator-emission' or
            bank['name']!='graph-ddr-bank-adapter' or
            bank['pass_identities'].get('load_images') is not True or
            bank['pass_identities'].get('controller_connected') is not False):
        raise ValueError('loaded simulator requires the verified native-memory peer boundary')
    compiled=verify_core_receipt(core,profile)
    joined=verify_ddr_transport_receipt(transport,profile)
    if Path(joined['core']).resolve()!=core or joined['core_identity']!=compiled['identity_sha256']:
        raise ValueError('loaded simulator transport belongs to a different core')
    identities={'core_identity':compiled['identity_sha256'],
                'transport_identity':joined['identity_sha256']}
    if any(stage['pass_identities'].get(key)!=identity
           for stage in (spec,native) for key,identity in identities.items()):
        raise ValueError('loaded simulator core/transport identity changed')
    if bank['tool_hashes']!=profile.tools['circt'].identities():
        raise ValueError('loaded adapter does not use the core HW compiler')
    for path in (core/'initialized/external-interface/interface.json',core/'ddr/layout.json'):
        if bank['input_hashes'].get(str(path))!=compiled['dependency_hashes'].get(str(path)):
            raise ValueError('loaded adapter interface/layout differs from the core')
    required={**compiled['dependency_hashes'],**joined['dependency_hashes'],
              **emitted,**adapted}
    for path in (core/'core.json',transport/'transport.json'):
        required[str(path)]=digest(path)
    if any(spec['input_hashes'].get(path)!=identity for path,identity in required.items()):
        raise ValueError('loaded simulator lacks transitive core/transport/emission provenance')
    base={**compiled['dependency_hashes'],**joined['dependency_hashes'],**adapted}
    for path in (core/'core.json',transport/'transport.json'):
        base[str(path)]=digest(path)
    manifest=json.loads((transport/'native-core/components/manifest.json').read_text())
    rtl=[transport/'native-core/library.sv',adapter/'rtl/graph_ddr_bank.sv',directory/'tb.sv']
    rtl.extend(transport/'native-core/components'/row['file'] for row in manifest['components'])
    origins=profile.document()['dependency_hashes']
    for name in ('closure_ddr_read_port.sv','closure_ddr_read_bank.sv','closure_ddr_read_cdc.sv',
                 'closure_ddr_image_loader.sv','closure_ddr_loaded_port.sv'):
        matches=[Path(path) for path in origins if Path(path).parts[-3:]==('rtl','primitives',name)]
        if len(matches)!=1:
            raise ValueError('compiled primitive origins are ambiguous or missing')
        rtl.append(matches[0])
    validate_native_inputs(native,base,rtl)
    generated=emission/'verilator'
    if (len(spec['argv'])!=6 or spec['argv'][1:]!=[
            '-C',str(generated),'-f','Vtb.mk','-j1']):
        raise ValueError('loaded simulator C++ build does not match native emission')
    if not all(flag in native['argv'] for flag in ('--cc','--exe','--timing','--assert')):
        raise ValueError('loaded simulator lacks native timing/assertion construction')
    if any(flag in native['argv'] for flag in ('--binary','--build')):
        raise ValueError('native emission must finish before C++ compilation')
    for path in generated.iterdir():
        if path.suffix in ('.cpp','.h','.mk') and path.is_file() and (path.is_symlink() or
                spec['input_hashes'].get(str(path))!=digest(path)):
            # The executable and build-produced object files are outputs of
            # make, not native emission. Source/header/makefile coverage is
            # the transitive construction boundary checked here.
            raise ValueError('loaded simulator generated source changed or untracked')
    binary=directory/filename
    if binary.is_symlink():
        raise ValueError('loaded simulator binary must be a regular file')
    dependencies.update(required)
    dependencies[str(Path(__file__).resolve())]=digest(Path(__file__).resolve())
    return binary,dependencies
