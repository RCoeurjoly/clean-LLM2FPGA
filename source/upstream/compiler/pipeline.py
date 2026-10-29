"""Declared native pass profile for imported integer PyTorch graphs.

Capture authenticates the graph before calling this backend. Each boundary is
serialized by its compiler; this driver runs tools and validates their artifacts in process.
"""
from dataclasses import dataclass
from pathlib import Path
import re

from compiler.stages import StageReceipt, StageSpec, digest, run_stage, invalidate_stage


TORCH_INTEGER_PASSES = (
    'builtin.module(llm2fpga-lower-exact-integer-ops,llm2fpga-lower-serial-gemv,'
    'torchdynamo-export-to-torch-backend-pipeline,llm2fpga-lower-exact-integer-ops,'
    'torch-backend-to-linalg-on-tensors-backend-pipeline,'
    'llm2fpga-check-standard-integer-ir)'
)

@dataclass(frozen=True)
class CompilerTool:
    executable: Path
    dependencies: tuple[Path, ...] = ()

    def identities(self):
        paths = (self.executable, *self.dependencies)
        if any(not path.is_absolute() for path in paths):
            raise ValueError('compiler tools and dependencies must use absolute paths')
        return {str(path): digest(path) for path in paths}


@dataclass(frozen=True)
class CompilationResult:
    profile: str
    output: Path
    receipts: tuple[StageReceipt, ...]


# Separate boundaries make both pass order and intermediate contracts visible.
# The structured stage retains loops; conversion to CFG is deliberately absent.
INTEGER_PROFILE = (
    ('standard', 'torch', ('--verify-each', '--pass-pipeline=' + TORCH_INTEGER_PASSES), 'standard.mlir'),
    ('buffers', 'torch', ('--verify-each', '--llm2fpga-fuse-pointwise',
                         '--llm2fpga-fold-producer-slices', '--canonicalize',
                         '--llm2fpga-protect-functional-inputs',
                         '--one-shot-bufferize=bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map',
                         '--buffer-results-to-out-params=add-result-attr=true modify-public-functions hoist-static-allocs'), 'buffers.mlir'),
    # Bufferization can leave unused backing allocations and views. Retire
    # them with the standard typed canonicalizer before lifetime analysis.
    ('scratch', 'torch', ('--verify-each', '--canonicalize', '--llm2fpga-reuse-buffers'), 'buffers.mlir'),
    ('loops', 'torch', ('--verify-each', '--refback-munge-memref-copy',
                       '--convert-linalg-to-loops', '--fold-memref-alias-ops',
                       '--lower-affine', '--arith-expand', '--llm2fpga-lower-integer-math',
                       '--llm2fpga-strength-reduce-integer-constants',
                       '--int-range-optimizations', '--canonicalize',
                       '--llm2fpga-compact-fixed-dim-storage'), 'loops.mlir'),

)


def integer_profile(*, entrypoint=None,
                    recover_restoring_sqrt_loops=False, coalesce_pointwise_pingpong=False,
                    fuse_gemv_epilogues=False, pool_private_capacity=False,
                    stable_scratch_pooling=False):
    """Select a native top function without altering serialized computation.

    An unspecified entrypoint retains native single-function inference. Linked
    generation modules declare their controller explicitly through this same
    profile, rather than replacing pass arguments in each caller.
    """
    if type(fuse_gemv_epilogues) is not bool or type(pool_private_capacity) is not bool:
        raise ValueError('memory reduction policies must be boolean')
    if type(stable_scratch_pooling) is not bool:
        raise ValueError('stable scratch pooling policy must be boolean')
    if stable_scratch_pooling and (entrypoint is None or not pool_private_capacity):
        raise ValueError('stable scratch pooling requires an entrypoint and capacity pooling')
    if type(coalesce_pointwise_pingpong) is not bool:
        raise ValueError('pointwise coalescing policy must be boolean')
    if type(recover_restoring_sqrt_loops) is not bool:
        raise ValueError('sqrt recovery policy must be boolean')
    profile = INTEGER_PROFILE
    def complete(stages):
        result = []
        for name, tool, arguments, filename in stages:
            if name == 'scratch':
                if stable_scratch_pooling:
                    arguments = tuple(a for a in arguments
                        if a != '--llm2fpga-reuse-buffers'
                        and not a.startswith('--llm2fpga-pool-sequential-scratch='))
                if pool_private_capacity:
                    arguments += (('--llm2fpga-pool-private-capacity=sharing-stable=true'
                                   if stable_scratch_pooling else '--llm2fpga-pool-private-capacity'),
                                  '--canonicalize')
            result.append((name, tool, arguments, filename))
        return tuple(result)

    if fuse_gemv_epilogues:
        profile = tuple((name, tool,
            arguments[:4] + ('--llm2fpga-fuse-gemv-epilogues', '--canonicalize',
                             '--linalg-fold-unit-extent-dims', '--canonicalize') + arguments[4:]
            if name == 'buffers' else arguments, filename)
            for name, tool, arguments, filename in profile)
    if coalesce_pointwise_pingpong:
        profile = tuple((name, tool,
            arguments[:2] + ('--llm2fpga-coalesce-pointwise-pingpong',) + arguments[2:]
            if name == 'scratch' else arguments, filename)
            for name, tool, arguments, filename in profile)
    if recover_restoring_sqrt_loops:
        profile = tuple((name, tool, arguments +
            (('--llm2fpga-recover-restoring-sqrt-loops', '--canonicalize')
             if name == 'loops' else ()), filename)
            for name, tool, arguments, filename in profile)
    if entrypoint is None:
        return complete(profile)
    if type(entrypoint) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.$-]*', entrypoint):
        raise ValueError('entrypoint must be an unquoted compiler symbol')
    selected = tuple((name, tool, arguments +
        (('--llm2fpga-pool-sequential-scratch=entrypoint=' + entrypoint,)
         if name == 'scratch' else ()), filename)
        for name, tool, arguments, filename in profile)
    return complete(selected)


def compile_integer_graph(raw_ir: Path, output_dir: Path,
                          tools: dict[str, CompilerTool], *, target='loops',
                          graph_inputs: Path | None = None,
                          timeout_seconds=120, entrypoint=None,
                          recover_restoring_sqrt_loops=False,
                          coalesce_pointwise_pingpong=False,
                          fuse_gemv_epilogues=False, pool_private_capacity=False,
                          stable_scratch_pooling=False) -> CompilationResult:
    """Run the integer backend, stopping only at a named verified boundary.

    """
    profile = integer_profile(entrypoint=entrypoint,
                              recover_restoring_sqrt_loops=recover_restoring_sqrt_loops,
                              coalesce_pointwise_pingpong=coalesce_pointwise_pingpong,
                              fuse_gemv_epilogues=fuse_gemv_epilogues,
                              pool_private_capacity=pool_private_capacity,
                              stable_scratch_pooling=stable_scratch_pooling)
    names = [stage[0] for stage in profile]
    if target not in names:
        raise ValueError(f'unknown target: {target}')
    output_dir = Path(output_dir).resolve()
    # A new frontend run invalidates every descendant, including stages beyond
    # an intermediate target. Old files cannot substitute for execution.
    owned_names = tuple(stage[0] for stage in integer_profile(
        entrypoint='main', pool_private_capacity=True, stable_scratch_pooling=True))
    for name in ('graph-inputs', *owned_names):
        invalidate_stage(output_dir / name)
    stages = profile[:names.index(target) + 1]
    missing = {stage[1] for stage in stages} - tools.keys()
    if missing:
        raise ValueError(f'missing compiler tools: {sorted(missing)}')
    source = Path(raw_ir).resolve(strict=True)
    receipts = []
    profile_identity = digest(Path(__file__))
    if graph_inputs is not None:
        manifest = Path(graph_inputs).resolve(strict=True)
        tool = tools['torch']
        filename = 'annotated.mlir'
        arguments = ('--verify-each', '--mlir-print-debuginfo',
            '--llm2fpga-annotate-graph-inputs=manifest=' + str(manifest) +
            ' manifest-sha256=' + digest(manifest))
        spec = StageSpec('graph-inputs',
            (str(tool.executable), str(source), *arguments, '-o', filename),
            {str(source): digest(source), str(manifest): digest(manifest)}, tool.identities(),
            {'profile': 'graph-integer-v1', 'profile_sha256': profile_identity},
            (filename,), timeout_seconds)
        receipts.append(run_stage(spec, output_dir / 'graph-inputs'))
        source = output_dir / 'graph-inputs' / filename
    for name, tool_name, arguments, filename in stages:
        tool = tools[tool_name]
        identities = tool.identities()
        inputs = {str(source): digest(source)}
        if tool_name in {'torch', 'circt', 'memory'}:
            # Native serialization retains PyTorch locations across IR files.
            arguments = ('--mlir-print-debuginfo', *arguments)
        spec = StageSpec(name, (str(tool.executable), str(source), *arguments, '-o', filename),
                         inputs, identities,
                         {'profile': 'graph-integer-v1', 'profile_sha256': profile_identity},
                         (filename,), timeout_seconds)
        receipts.append(run_stage(spec, output_dir / name))
        source = output_dir / name / filename
    return CompilationResult('graph-integer-v1', source, tuple(receipts))
