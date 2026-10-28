"""Compile the complete declared graph core from authenticated model inputs."""
import argparse
import math
from pathlib import Path


def _positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('timeout must be positive finite seconds')
    return int(number) if number.is_integer() else number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'package', 'numerical-contract', 'output', 'calyx-library'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('torch', 'circt', 'memory', 'translate', 'calyx'):
        parser.add_argument('--' + name + '-tool', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=_positive_seconds, default=600.0)
    parser.add_argument('--memory-implementation', choices=('register', 'bram', 'lutram'),
                        default='bram')
    parser.add_argument('--narrow-immutable', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--fuse-gemv-epilogues', action='store_true',
                        help='Fuse exact tensor GEMV epilogues and fold unit dimensions')
    parser.add_argument('--pool-private-capacity', action='store_true',
                        help='Pool fully initialized private scratch by capacity')
    parser.add_argument('--stable-scratch-pooling', action='store_true',
                        help='Use stable banks and normalize index scales for structural sharing')
    parser.add_argument('--coalesce-pointwise-pingpong', action='store_true',
                        help='Coalesce proven private adjacent same-element buffer maps')
    parser.add_argument('--recover-restoring-sqrt-loops', action='store_true',
                        help='Recover exact restoring-root tails as sequential loops')
    parser.add_argument('--offchip-immutable', action='store_true',
                        help='Externalize full-width immutable reference groups with dynamic completion')
    parser.add_argument('--controller-stage', type=Path,
                        help='Authenticated native LiteDRAM export to connect downstream')
    parser.add_argument('--yosys-tool', type=Path,
                        help='Pinned native domain elaborator required for DDR transport')
    args=parser.parse_args(argv)
    if args.stable_scratch_pooling and not args.pool_private_capacity:
        parser.error('stable-scratch-pooling requires pool-private-capacity')
    if (args.controller_stage is None)!=(args.yosys_tool is None):
        parser.error('controller-stage and yosys-tool must be supplied together')
    if args.controller_stage is not None and (not args.offchip_immutable or args.narrow_immutable):
        parser.error('controller transport requires offchip-immutable and no-narrow-immutable')
    return args


def main(argv=None):
    args = parse_arguments(argv)
    from .frontend.package_inputs import load_compile_inputs
    from .pipeline import CompilerTool
    from .build_profile import CoreBuildProfile
    from .build_core import build_core, verify_core_receipt, invalidate_core_receipts
    invalidate_core_receipts(args.output)
    inputs = load_compile_inputs(args.snapshot, args.package, args.numerical_contract)
    tools = {name: CompilerTool(getattr(args, name + '_tool'))
             for name in ('torch', 'circt', 'memory', 'translate', 'calyx')}
    profile = CoreBuildProfile(args.numerical_contract, tools, args.calyx_library,
                               memory_implementation=args.memory_implementation,
                               narrow_immutable=args.narrow_immutable,
                               offchip_immutable=args.offchip_immutable,
                               recover_restoring_sqrt_loops=args.recover_restoring_sqrt_loops,
                               coalesce_pointwise_pingpong=args.coalesce_pointwise_pingpong,
                               fuse_gemv_epilogues=args.fuse_gemv_epilogues,
                               pool_private_capacity=args.pool_private_capacity,
                               stable_scratch_pooling=args.stable_scratch_pooling)
    receipt = build_core(inputs, profile, args.output, timeout_seconds=args.timeout_seconds)
    verify_core_receipt(args.output, profile)
    identity=receipt.identity_sha256
    if args.controller_stage is not None:
        from .ddr_transport import build_ddr_transport
        transport=build_ddr_transport(args.output,profile,args.controller_stage,
            args.output/'transport',yosys=args.yosys_tool,timeout_seconds=args.timeout_seconds)
        identity=transport['identity_sha256']
    print(identity)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
