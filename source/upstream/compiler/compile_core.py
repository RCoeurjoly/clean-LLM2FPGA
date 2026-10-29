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
    for name in ('snapshot', 'package', 'numerical-contract', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--torch-tool', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=_positive_seconds, default=600.0)
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
    args=parser.parse_args(argv)
    if args.stable_scratch_pooling and not args.pool_private_capacity:
        parser.error('stable-scratch-pooling requires pool-private-capacity')
    return args


def main(argv=None):
    args = parse_arguments(argv)
    from .frontend.package_inputs import load_compile_inputs
    from .pipeline import CompilerTool
    from .build_profile import CoreBuildProfile
    from .build_core import build_core
    inputs = load_compile_inputs(args.snapshot, args.package, args.numerical_contract)
    tools = {'torch': CompilerTool(args.torch_tool)}
    profile = CoreBuildProfile(args.numerical_contract, tools,
                               recover_restoring_sqrt_loops=args.recover_restoring_sqrt_loops,
                               coalesce_pointwise_pingpong=args.coalesce_pointwise_pingpong,
                               fuse_gemv_epilogues=args.fuse_gemv_epilogues,
                               pool_private_capacity=args.pool_private_capacity,
                               stable_scratch_pooling=args.stable_scratch_pooling)
    output = build_core(inputs, profile, args.output, timeout_seconds=args.timeout_seconds)
    print(f'Compiled controller: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
