"""Join authenticated graph compilation to native DDR transport without answer inputs.

The receipt proves emitted wiring and direct memory clock/reset aliases. Complete
model execution, host protocols, PHY calibration and physical fit remain separate
acceptance gates. Controller initialization flags never substitute for calibration.
"""
import json
import re
import uuid
from pathlib import Path
from .build_core import verify_core_receipt
from .clock_contract import qualify_external_clock_contract
from .ddr_bank_adapter import emit_ddr_bank_adapter_stage
from .stages import digest, verify_stage_receipt, _identity, _write_json, StageSpec, run_stage
from .pipeline import CALYX_RESOURCE_PASSES


def invalidate_ddr_transport(output):
    output=Path(output).resolve()
    for name in ('transport.json','domains/qualification.json',
                 'domains/elaboration/success.json','adapter/emission/success.json','native-core/success.json'):
        (output/name).unlink(missing_ok=True)


def build_ddr_transport(core, profile, controller, output, *, yosys, timeout_seconds=120):
    """Consume a checked common core and native controller export, then emit HW joins."""
    core,controller,output=(Path(p).resolve() for p in (core,controller,output))
    # Reject destructive placement before invalidating only our owned receipts.
    if core.is_relative_to(output) or controller.is_relative_to(output):
        raise ValueError('transport output overlaps core/controller inputs')
    invalidate_ddr_transport(output)
    if not profile.offchip_immutable:
        raise ValueError('DDR transport requires off-chip immutable profile')
    core_receipt=verify_core_receipt(core,profile)
    controller_deps=verify_stage_receipt(controller)
    source=Path(core_receipt['output'])
    interface=core/'initialized/external-interface/interface.json'
    layout=core/'ddr/layout.json'
    for path in (interface,layout,source):
        if str(path) not in core_receipt['dependency_hashes']:
            raise ValueError('core transport input lacks compilation provenance')
    for name in ('controller.json','controller.v','initialization.json','csr.json'):
        if str(controller/name) not in controller_deps:
            raise ValueError('native controller export missing declared artifact')
    # Re-emit the same bound Futil with the native synthesis backend. The core
    # diagnostic SV contains simulation assertions that Yosys cannot elaborate.
    # Native component serialization keeps the entrypoint domain check bounded;
    # all component files are authenticated below, without splitting RTL text.
    synthesis=output/'native-core'
    components=synthesis/'components'
    if components.exists():
        components.rename(synthesis/('components.previous-'+uuid.uuid4().hex))
    futil=core/'initialized/futil/kernel.futil'
    if str(futil) not in core_receipt['dependency_hashes']:
        raise ValueError('bound Futil lacks core provenance')
    tool=profile.tools['calyx']
    native_inputs=dict(core_receipt['dependency_hashes'])
    native_inputs[str(Path(__file__).resolve())]=digest(Path(__file__).resolve())
    run_stage(StageSpec('ddr-native-core-export',
        (str(tool.executable),str(futil),'-b','verilog',*CALYX_RESOURCE_PASSES,
         '-p','external','-l',str(profile.calyx_library),'--synthesis',
         '-o','library.sv','-x','verilog-component-dir=components'),
        native_inputs,tool.identities(),
        {'scope':'Native synthesis-mode serialization of the unchanged bound graph core'},
        ('library.sv','components/manifest.json'),timeout_seconds),synthesis)
    native_deps=verify_stage_receipt(synthesis)
    manifest=json.loads((components/'manifest.json').read_text())
    entrypoint=json.loads(interface.read_text())['entrypoint']
    records=manifest.get('components',[])
    if (manifest.get('schema_version')!=1 or manifest.get('entrypoint')!=entrypoint or
            manifest.get('library')!='library.sv' or not records or
            len({row['component'] for row in records})!=len(records) or
            len({row['file'] for row in records})!=len(records)):
        raise ValueError('invalid native component manifest')
    for row in records:
        if not re.fullmatch(r'component-[0-9]+\.sv',row['file']):
            raise ValueError('invalid native component output path')
        path=components/row['file']
        if path.is_symlink():raise ValueError('native component must be a regular output')
        native_deps[str(path)]=digest(path)
    matches=[components/row['file'] for row in records if row['component']==entrypoint]
    if len(matches)!=1:raise ValueError('native component entrypoint missing')
    source=matches[0]
    qualification=qualify_external_clock_contract(source,interface,output/'domains',
        yosys,timeout_seconds=timeout_seconds)
    emitted=emit_ddr_bank_adapter_stage(interface,layout,output/'adapter',profile.tools['circt'],
        controller=controller/'controller.json',load_images=True,timeout_seconds=timeout_seconds)
    # Authenticate every native component after the subsequent tool boundaries.
    if any(digest(Path(name))!=identity for name,identity in native_deps.items()):
        raise ValueError('native component changed during transport build')
    # Inputs must still authenticate after native execution, including model images.
    verify_core_receipt(core,profile)
    verify_stage_receipt(controller)
    dependencies=dict(core_receipt['dependency_hashes'])
    dependencies.update(controller_deps)
    dependencies.update(native_deps)
    for stage in (output/'native-core',output/'domains/elaboration',output/'adapter/emission'):
        dependencies.update(verify_stage_receipt(stage))
        for name in ('spec.json','success.json'):
            dependencies[str(stage/name)]=digest(stage/name)
    for path in (core/'core.json',controller/'spec.json',controller/'success.json',
                 output/'domains/qualification.json',Path(__file__).resolve()):
        dependencies[str(path)]=digest(path)
    body={'schema_version':1,'core':str(core),'controller':str(controller),
        'core_identity':core_receipt['identity_sha256'],'profile_sha256':profile.identity_sha256,
        'domain_identity':qualification['elaboration_identity'],
        'emission_identity':emitted.identity_sha256,
        'output':str(output/'adapter/emission/rtl/graph_ddr_bank.sv'),
        'dependency_hashes':dependencies,
        'scope':'Authenticated emitted bank/CDC/loader/controller wiring and direct memory domains only; no complete-model numerical, calibration or physical acceptance'}
    receipt=dict(body,identity_sha256=_identity(body))
    _write_json(output/'transport.json',receipt)
    try:
        verify_ddr_transport_receipt(output,profile)
    except Exception:
        (output/'transport.json').unlink(missing_ok=True)
        raise
    return receipt


def verify_ddr_transport_receipt(output, profile):
    output=Path(output).resolve()
    receipt=json.loads((output/'transport.json').read_text())
    body={key:value for key,value in receipt.items() if key!='identity_sha256'}
    profile.validate()
    if (receipt['schema_version']!=1 or not profile.offchip_immutable or
            receipt['profile_sha256']!=profile.identity_sha256 or
            receipt['identity_sha256']!=_identity(body) or
            receipt['output']!=str(output/'adapter/emission/rtl/graph_ddr_bank.sv')):
        raise ValueError('invalid DDR transport receipt')
    dependencies=receipt['dependency_hashes']
    for name,expected in dependencies.items():
        path=Path(name)
        # Native stage outputs and core inputs reject symlinks in their own
        # verifiers. Tool inputs may use pinned Nix executable symlinks; their
        # resolved content must retain the declared hash.
        if not path.is_absolute() or not path.is_file() or digest(path)!=expected:
            raise ValueError('transport dependency changed or missing: '+name)
    core=verify_core_receipt(Path(receipt['core']),profile)
    if core['identity_sha256']!=receipt['core_identity']:
        raise ValueError('transport core identity changed')
    required=dict(core['dependency_hashes'])
    for stage in (Path(receipt['controller']),output/'native-core',output/'domains/elaboration',output/'adapter/emission'):
        required.update(verify_stage_receipt(stage))
        for name in ('spec.json','success.json'):required[str(stage/name)]=digest(stage/name)
    required[str(Path(receipt['core'])/'core.json')]=digest(Path(receipt['core'])/'core.json')
    required[str(output/'domains/qualification.json')]=digest(output/'domains/qualification.json')
    required[str(Path(__file__).resolve())]=digest(Path(__file__).resolve())
    manifest=json.loads((output/'native-core/components/manifest.json').read_text())
    for row in manifest['components']:
        if not re.fullmatch(r'component-[0-9]+\.sv',row['file']):
            raise ValueError('invalid native component output path')
        path=output/'native-core/components'/row['file']
        required[str(path)]=digest(path)
    if any(dependencies.get(name)!=expected for name,expected in required.items()):
        raise ValueError('transport transitive dependency missing or inconsistent')
    if (json.loads((output/'adapter/emission/success.json').read_text())['identity_sha256']!=receipt['emission_identity'] or
            json.loads((output/'domains/elaboration/success.json').read_text())['identity_sha256']!=receipt['domain_identity']):
        raise ValueError('transport stage identity differs')
    return receipt
