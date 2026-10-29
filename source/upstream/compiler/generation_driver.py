"""Link verified graph-derived functions using native MLIR passes.

This boundary accepts compiler artifacts, not golden outputs. It checks completed stages
and file identities; model computation is never parsed or rewritten.
The frontend remains responsible for capturing and importing PyTorch graphs.
"""
from dataclasses import dataclass
from pathlib import Path

from .pipeline import CompilationResult, CompilerTool, integer_profile
from .stages import StageSpec, digest, run_stage, verified_stage, verify_stage_receipt, invalidate_stage


@dataclass(frozen=True)
class ContextFunction:
    length: int
    buffer_stage: Path


def _verified_artifact(directory, stage_names, filename):
    directory = directory.resolve()
    try:
        spec, _ = verified_stage(directory)
        if spec.name not in stage_names or spec.outputs != (filename,):
            raise ValueError('unexpected compiler stage artifact')
        source = directory / filename
        # The MLIR list-option grammar uses comma and whitespace delimiters.
        if any(c in str(source) for c in ', \t\r\n'):
            raise ValueError('context path contains a list-option delimiter')
        return source, verify_stage_receipt(directory)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f'verified compiler boundary: {error}') from error


def _verified_buffer(context):
    try:
        return _verified_artifact(context.buffer_stage,
                                  ('buffers', 'generation-buffers'), 'buffers.mlir')
    except ValueError as error:
        raise ValueError(f'context {context.length}: verified buffer boundary: {error}') from error


def lower_generation_controller(link_stage: Path, output_dir: Path,
                                tools: dict[str, CompilerTool], *, timeout_seconds=120,
                                recover_restoring_sqrt_loops=False,
                                coalesce_pointwise_pingpong=False,
                                fuse_gemv_epilogues=False, pool_private_capacity=False,
                                stable_scratch_pooling=False):
    """Lower an authenticated native link through the controller loops boundary.

    This boundary consumes computation IR and provenance, never expected tokens.
    Initialized-memory export and numerical acceptance remain separate stages.
    """
    stages = tuple(stage for stage in integer_profile(
        entrypoint='main',
        recover_restoring_sqrt_loops=recover_restoring_sqrt_loops,
        coalesce_pointwise_pingpong=coalesce_pointwise_pingpong,
        fuse_gemv_epilogues=fuse_gemv_epilogues, pool_private_capacity=pool_private_capacity,
        stable_scratch_pooling=stable_scratch_pooling)
        if stage[0] not in {'standard', 'buffers', 'futil', 'sv'})
    stages = stages[:next(i for i, stage in enumerate(stages) if stage[0] == 'loops') + 1]
    output_dir = Path(output_dir).resolve()
    owned_stages = integer_profile(entrypoint='main', pool_private_capacity=True,
                                   stable_scratch_pooling=True)
    for name, _, _, _ in owned_stages:
        if name in {'standard', 'buffers', 'futil', 'sv'}:
            continue
        invalidate_stage(output_dir / name)
    source, dependencies = _verified_artifact(Path(link_stage),
                                               ('generation-link',), 'linked.mlir')
    missing = {tool for _, tool, _, _ in stages} - tools.keys()
    if missing:
        raise ValueError(f'missing compiler tools: {sorted(missing)}')
    receipts = []
    profile_hash = digest(Path(__file__).with_name('pipeline.py'))
    for name, tool_name, arguments, filename in stages:
        tool = tools[tool_name]
        spec = StageSpec(name,
            (str(tool.executable), str(source), '--mlir-print-debuginfo',
             *arguments, '-o', filename),
            {**dependencies, str(source): digest(source)}, tool.identities(),
            {'profile': 'graph-generation-controller-v1',
             'profile_sha256': profile_hash, 'driver_sha256': digest(Path(__file__))},
            (filename,), timeout_seconds)
        receipts.append(run_stage(spec, output_dir / name))
        source = output_dir / name / filename
    return CompilationResult('graph-generation-controller-v1', source, tuple(receipts))


def link_generation_functions(contexts: tuple[ContextFunction, ...], output_dir: Path,
                              tool: CompilerTool, *, timeout_seconds=120) -> CompilationResult:
    """Authenticate all sixteen predecessors before executing import and link.

    The native linker validates geometry, frozen inputs and output markers.
    Numerical RTL acceptance is a separate comparison step.
    """
    if (type(contexts) is not tuple or
            tuple(item.length for item in contexts) != tuple(range(4, 20)) or
            any(type(item.length) is not int for item in contexts)):
        raise ValueError('generation requires ordered unique contexts 4 through 19')
    output_dir = Path(output_dir).resolve()
    stage = output_dir / 'linked'
    # A failed rerun cannot leave an earlier execution authoritative.
    invalidate_stage(stage)
    sources, inputs = [], {}
    for context in contexts:
        source, dependencies = _verified_buffer(context)
        sources.append(source)
        inputs.update(dependencies)
    tool_identities = tool.identities()
    output_dir.mkdir(parents=True, exist_ok=True)
    seed = output_dir / 'empty.mlir'
    seed.write_text('module {}\n')
    inputs[str(seed)] = digest(seed)
    arguments = ('--verify-each', '--mlir-print-debuginfo',
                 '--llm2fpga-import-generation=files=' + ','.join(map(str, sources)),
                 '--llm2fpga-link-generation')
    spec = StageSpec('generation-link',
        (str(tool.executable), str(seed), *arguments, '-o', 'linked.mlir'),
        inputs, tool_identities,
        {'profile': 'graph-generation-link-v1',
         'driver_sha256': digest(Path(__file__))},
        ('linked.mlir',), timeout_seconds)
    receipt = run_stage(spec, stage)
    return CompilationResult('graph-generation-link-v1', stage / 'linked.mlir', (receipt,))
