#!/usr/bin/env python3
"""Export native synthesis RTL downstream of the authenticated common graph core.

This stage changes backend emission mode, without editing generated source.
It establishes compilation provenance, not numerical or physical acceptance.
"""
import argparse
import json
from pathlib import Path
from compiler.build_core import verify_core_receipt
from compiler.build_profile import CoreBuildProfile
from compiler.generation_driver import _verified_artifact
from compiler.pipeline import CompilerTool, CALYX_RESOURCE_PASSES
from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt, _identity, _write_json


def export_core_synthesis(core, profile, output, *, timeout_seconds=1800):
    core, output = Path(core).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    acceptance = output/'core-synthesis.json'
    acceptance.unlink(missing_ok=True)
    receipt = verify_core_receipt(core, profile)
    # The common core already validated and bound the exact initialized Futil.
    # Reuse that authenticated artifact; only the native emission mode changes.
    source, native_inputs = _verified_artifact(core/'initialized/futil',
        ('futil',), 'kernel.futil')
    dependencies = dict(native_inputs)
    dependencies.update(receipt['dependency_hashes'])
    dependencies[str(core/'core.json')] = digest(core/'core.json')
    implementation = Path(__file__).resolve()
    dependencies[str(implementation)] = digest(implementation)
    interface = core/'initialized/external-interface/interface.json'
    if receipt['dependency_hashes'].get(str(interface)) != digest(interface):
        raise ValueError('core does not authenticate its external interface')
    library = {str(p): digest(p) for p in sorted(profile.calyx_library.rglob('*'))
               if p.is_file()}
    if not library:
        raise ValueError('Calyx library is empty')
    # Downstream consumers stage relative initializers from these identities.
    native_inputs.update(receipt['dependency_hashes'])
    native_inputs.update(library)
    native_inputs[str(core/'core.json')] = dependencies[str(core/'core.json')]
    native_inputs[str(implementation)] = dependencies[str(implementation)]
    tool = profile.tools['calyx']
    # The pinned backend suppresses simulation checks in both modes. Flat
    # native emission bounds expressions that become megabyte ASTs in nested
    # mode; no generated source is parsed or edited here.
    run_stage(StageSpec('sv',
        (str(tool.executable), str(source), '-b', 'verilog', *CALYX_RESOURCE_PASSES,
         '-p', 'external', '-l', str(profile.calyx_library), '--synthesis',
         '-o', 'kernel.sv'), native_inputs, tool.identities(),
        {'profile_sha256': profile.identity_sha256, 'backend': 'native-flat-synthesis'},
        ('kernel.sv',), timeout_seconds), output/'sv')
    verify_core_receipt(core, profile)
    dependencies.update(verify_stage_receipt(output/'sv'))
    for name, expected in dependencies.items():
        if digest(Path(name)) != expected:
            raise ValueError('synthesis dependency changed: '+name)
    document = {'schema_version': 1, 'core_identity_sha256': receipt['identity_sha256'],
        'profile_sha256': profile.identity_sha256, 'output': str(output/'sv/kernel.sv'),
        'external_interface': str(interface), 'backend': 'native-flat-synthesis',
        'dependency_hashes': dependencies,
        'scope': 'Native synthesis export only; numerical, DDR3, fit and board gates are downstream.'}
    document['identity_sha256'] = _identity(document)
    _write_json(acceptance, document)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('core', 'output', 'numerical-contract', 'calyx-library'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    args = parser.parse_args(argv)
    # Invalidate acceptance before profile authentication as well.
    (args.output/'core-synthesis.json').unlink(missing_ok=True)
    doc = json.loads((args.core/'profile.json').read_text())
    tools = {name: CompilerTool(Path(next(iter(identities))),
                tuple(Path(path) for path in list(identities)[1:]))
             for name, identities in doc['tool_hashes'].items()}
    profile = CoreBuildProfile(args.numerical_contract, tools, args.calyx_library,
        memory_implementation=doc['memory_policy']['implementation'],
        narrow_immutable=doc['memory_policy']['narrow_immutable'],
        offchip_immutable=doc['memory_policy'].get('offchip_immutable', False),
        lut_memory_max_bits=doc['memory_policy'].get('lut_memory_max_bits',0),
        recover_restoring_sqrt_loops=doc.get('optimization_policy', {}).get(
            'recover_restoring_sqrt_loops', False),
        coalesce_pointwise_pingpong=doc.get('optimization_policy', {}).get(
            'coalesce_pointwise_pingpong', False),
        fuse_gemv_epilogues=doc.get('optimization_policy', {}).get('fuse_gemv_epilogues', False),
        pool_private_capacity=doc.get('optimization_policy', {}).get('pool_private_capacity', False),
        stable_scratch_pooling=doc.get('optimization_policy', {}).get('stable_scratch_pooling', False))
    print(export_core_synthesis(args.core, profile, args.output,
        timeout_seconds=args.timeout_seconds)['identity_sha256'])


if __name__ == '__main__':
    main()
