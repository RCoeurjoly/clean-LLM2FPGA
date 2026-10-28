#!/usr/bin/env python3
"""Join an authenticated common graph core to the native board controller.

The outer native stage authenticates this driver before execution. Its child
uses the existing typed HW adapter and unchanged bound graph computation.
Numerical execution, host control, calibration and physical fit are separate.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from compiler.build_core import verify_core_receipt
from compiler.build_profile import CoreBuildProfile
from compiler.ddr_transport import (build_ddr_transport, invalidate_ddr_transport,
                                    verify_ddr_transport_receipt)
from compiler.pipeline import CompilerTool
from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt


def read_profile(core, numerical_contract, calyx_library):
    document = json.loads((Path(core) / 'profile.json').read_text())
    tools = {name: CompilerTool(Path(next(iter(identities))),
                tuple(Path(path) for path in list(identities)[1:]))
             for name, identities in document['tool_hashes'].items()}
    policy = document['memory_policy']
    return CoreBuildProfile(numerical_contract, tools, calyx_library,
        memory_implementation=policy['implementation'],
        narrow_immutable=policy['narrow_immutable'],
        offchip_immutable=policy.get('offchip_immutable', False),
        lut_memory_max_bits=policy.get('lut_memory_max_bits',0),
        recover_restoring_sqrt_loops=document.get('optimization_policy', {}).get(
            'recover_restoring_sqrt_loops', False),
        coalesce_pointwise_pingpong=document.get('optimization_policy', {}).get(
            'coalesce_pointwise_pingpong', False),
        fuse_gemv_epilogues=document.get('optimization_policy', {}).get('fuse_gemv_epilogues', False),
        pool_private_capacity=document.get('optimization_policy', {}).get('pool_private_capacity', False),
        stable_scratch_pooling=document.get('optimization_policy', {}).get('stable_scratch_pooling', False))


def export_transport_stage(core, controller, output, *, numerical_contract,
                           calyx_library, python, yosys, timeout_seconds=1800):
    core, controller, output = (Path(path).resolve() for path in (core, controller, output))
    if core.is_relative_to(output) or controller.is_relative_to(output):
        raise ValueError('transport export output overlaps core/controller input')
    # Input/profile authentication may fail before run_stage is reached.
    (output / 'success.json').unlink(missing_ok=True)
    invalidate_ddr_transport(output / 'transport')
    profile = read_profile(core, numerical_contract, calyx_library)
    receipt = verify_core_receipt(core, profile)
    dependencies = dict(receipt['dependency_hashes'])
    dependencies.update(verify_stage_receipt(controller))
    driver = Path(__file__).resolve()
    for path in (driver, core / 'core.json', controller / 'spec.json',
                 controller / 'success.json'):
        dependencies[str(path)] = digest(path)
    python, yosys = Path(python).absolute(), Path(yosys).absolute()
    argv = (str(python), str(driver), '--native-join', '--core', str(core),
            '--controller', str(controller), '--output', 'transport',
            '--numerical-contract', str(Path(numerical_contract).resolve()),
            '--calyx-library', str(Path(calyx_library).resolve()),
            '--yosys', str(yosys), '--timeout-seconds', str(timeout_seconds))
    result = run_stage(StageSpec('graph-core-ddr-transport', argv, dependencies,
        {str(tool): digest(tool) for tool in (python, yosys)},
        {'core_identity': receipt['identity_sha256'],
         'scope': 'Native graph/controller transport composition only'},
        ('transport/transport.json',), timeout_seconds,
        environment={'PYTHONPATH': os.environ.get('PYTHONPATH', ''),
                     'PYTHONDONTWRITEBYTECODE': '1',
                     'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}), output)
    try:
        verify_ddr_transport_receipt(output / 'transport', profile)
    except Exception:
        (output / 'success.json').unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('core', 'controller', 'output', 'numerical-contract', 'calyx-library', 'yosys'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    parser.add_argument('--native-join', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.native_join:
        output = args.output.resolve()
        if args.core.resolve().is_relative_to(output) or args.controller.resolve().is_relative_to(output):
            raise ValueError('transport export output overlaps core/controller input')
        invalidate_ddr_transport(args.output)
        profile = read_profile(args.core, args.numerical_contract, args.calyx_library)
        receipt = build_ddr_transport(args.core, profile, args.controller, args.output,
            yosys=args.yosys, timeout_seconds=args.timeout_seconds)
        print(receipt['identity_sha256'])
    else:
        receipt = export_transport_stage(args.core, args.controller, args.output,
            numerical_contract=args.numerical_contract, calyx_library=args.calyx_library,
            python=Path(sys.executable), yosys=args.yosys,
            timeout_seconds=args.timeout_seconds)
        print(receipt.identity_sha256)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
