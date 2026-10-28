"""Declared native pass profile for imported integer PyTorch graphs.

Capture authenticates the graph before calling this backend. Each boundary is
serialized by its compiler; this driver only runs tools and records receipts.
"""
from dataclasses import dataclass
from pathlib import Path
import re

from compiler.stages import StageReceipt, StageSpec, digest, run_stage


TORCH_INTEGER_PASSES = (
    'builtin.module(llm2fpga-lower-exact-integer-ops,llm2fpga-lower-serial-gemv,'
    'torchdynamo-export-to-torch-backend-pipeline,llm2fpga-lower-exact-integer-ops,'
    'torch-backend-to-linalg-on-tensors-backend-pipeline,'
    'llm2fpga-check-standard-integer-ir)'
)

# Exclusive schedules can still create a physical combinational cycle when
# operators used in opposite orders are merged. Keep Calyx's combinational
# sharing class unmerged; register and state_share bounds remain unrestricted.
# Retire cells made dead by sharing before analyzing the next component.
# This composes the native dead-cell pass; no serialized design is rewritten.
CALYX_RESOURCE_PASSES = ('-x', 'cell-share:bounds=1,-1,-1',
                         '-x', 'cell-share:retire-dead-cells=true',
                         '-x', 'simplify-with-control:retire-dead-groups=true',
                         '-x', 'static-promotion:retire-dead-groups=true')


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
    ('structured', 'torch', ('--verify-each', '--canonicalize', '--scf-for-to-while', '--canonicalize'), 'structured.mlir'),
    ('flat-memory', 'memory', ('--verify-each', '--flatten-memref', '--canonicalize'), 'flat-memory.mlir'),
    ('calyx-ir', 'memory', ('--verify-each', '--lower-scf-to-calyx', '--symbol-dce'), 'calyx.mlir'),
    ('memory-contracts', 'memory', ('--verify-each', '--llm2fpga-complete-calyx-memory-contracts'), 'calyx.mlir'),
    ('futil', 'translate', ('--export-calyx',), 'kernel.futil'),
    ('sv', 'calyx', ('-b', 'verilog', *CALYX_RESOURCE_PASSES), 'kernel.sv'),
)


def integer_profile(*, entrypoint=None, bytecode=False, runtime_roles=False,
                    recover_restoring_sqrt_loops=False, coalesce_pointwise_pingpong=False,
                    fuse_gemv_epilogues=False, pool_private_capacity=False,
                    stable_scratch_pooling=False):
    """Select a native top function without altering serialized computation.

    An unspecified entrypoint retains native single-function inference. Linked
    generation modules declare their controller explicitly through this same
    profile, rather than replacing pass arguments in each caller.
    Production controllers retain native Calyx bytecode; text remains available
    for the smaller generic kernel diagnostics. Both use the same typed passes.
    Runtime role resolution is enabled for generation controllers: it validates
    and locates their prompt, generated-token and final-logits physical memories.
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
    if type(bytecode) is not bool:
        raise ValueError('bytecode policy must be boolean')
    if type(runtime_roles) is not bool:
        raise ValueError('runtime role policy must be boolean')
    if runtime_roles and entrypoint is None:
        raise ValueError('runtime roles require an explicit generation entrypoint')
    profile = tuple((name, tool,
        arguments + (('--llm2fpga-resolve-runtime-memories',)
                     if runtime_roles and name == 'memory-contracts' else ()) +
                    (('--emit-bytecode',)
                     if bytecode and name in ('calyx-ir', 'memory-contracts') else ()),
        'calyx.mlirbc' if bytecode and name in ('calyx-ir', 'memory-contracts') else filename)
        for name, tool, arguments, filename in INTEGER_PROFILE)
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
    selected = tuple((name, tool, tuple(
        '--lower-scf-to-calyx=top-level-function=' + entrypoint
        if argument == '--lower-scf-to-calyx' else argument
        for argument in arguments) +
        (('--llm2fpga-pool-sequential-scratch=entrypoint=' + entrypoint,)
         if name == 'scratch' else ()), filename)
        for name, tool, arguments, filename in profile)
    result = []
    for stage in selected:
        result.append(stage)
        if stage[0] == 'flat-memory':
            if stable_scratch_pooling:
                result.append(('normalized-index-scales', 'torch',
                    ('--verify-each', '--llm2fpga-normalize-index-scales-for-sharing'),
                    'flat-memory.mlir'))
            result.append(('shared-functions', 'torch',
                ('--verify-each', '--llm2fpga-share-private-functions=entrypoint=' + entrypoint),
                'shared-functions.mlir'))
            if stable_scratch_pooling:
                result.append(('reduced-index-scales', 'torch',
                    ('--verify-each', '--llm2fpga-strength-reduce-integer-constants=index-scales=true',
                     '--canonicalize'), 'shared-functions.mlir'))
    return complete(result)


def compile_integer_graph(raw_ir: Path, output_dir: Path,
                          tools: dict[str, CompilerTool], *, target='sv',
                          calyx_library: Path | None = None, graph_inputs: Path | None = None,
                          timeout_seconds=120, entrypoint=None,
                          recover_restoring_sqrt_loops=False,
                          coalesce_pointwise_pingpong=False,
                          fuse_gemv_epilogues=False, pool_private_capacity=False,
                          stable_scratch_pooling=False) -> CompilationResult:
    """Run the integer backend, stopping only at a named verified boundary.

    Intermediate targets are diagnostic artifacts, not RTL acceptance. The SV
    target always executes the memory-contract pass and native Calyx verifier.
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
    # an intermediate target. Old files remain diagnostics, never acceptance.
    owned_names = tuple(stage[0] for stage in integer_profile(
        entrypoint='main', pool_private_capacity=True, stable_scratch_pooling=True))
    for name in ('graph-inputs', *owned_names):
        (output_dir / name / 'success.json').unlink(missing_ok=True)
    stages = profile[:names.index(target) + 1]
    missing = {stage[1] for stage in stages} - tools.keys()
    if missing:
        raise ValueError(f'missing compiler tools: {sorted(missing)}')
    library_inputs = {}
    if target == 'sv':
        if calyx_library is None or not calyx_library.is_absolute() or not calyx_library.is_dir():
            raise ValueError('SV compilation requires an absolute Calyx library directory')
        library_inputs = {str(path): digest(path) for path in sorted(calyx_library.rglob('*'))
                          if path.is_file()}
        if not library_inputs:
            raise ValueError('Calyx library is empty')
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
        if name == 'sv':
            arguments += ('-l', str(calyx_library))
            inputs.update(library_inputs)
        spec = StageSpec(name, (str(tool.executable), str(source), *arguments, '-o', filename),
                         inputs, identities,
                         {'profile': 'graph-integer-v1', 'profile_sha256': profile_identity},
                         (filename,), timeout_seconds)
        receipts.append(run_stage(spec, output_dir / name))
        source = output_dir / name / filename
    return CompilationResult('graph-integer-v1', source, tuple(receipts))


def bind_initialized_memories(calyx_ir: Path, manifest: Path, primitive_source: Path,
                              output_dir: Path, tools: dict[str, CompilerTool], *,
                              calyx_library: Path, synthesis=False, runtime_interface=False,
                              timeout_seconds=120) -> CompilationResult:
    """Validate and bind typed memories before native Futil and SV export.

    Images are declared inputs, not generated results. The native validation
    pass checks their geometry and manifest identities; all subsequent stages
    retain those dependencies so changing an image invalidates acceptance.
    Bound IR uses native MLIR bytecode to avoid expanding its typed attributes
    into a large text intermediate before the native Calyx serializer.
    This driver never parses or edits emitted computation.
    """
    import json
    output_dir = Path(output_dir).resolve()
    names = ('memory-validation', 'memory-binding', 'external-interface', 'futil', 'sv')
    for name in names:
        (output_dir / name / 'success.json').unlink(missing_ok=True)
    if type(runtime_interface) is not bool:
        raise ValueError('runtime interface policy must be boolean')
    manifest = Path(manifest).resolve(strict=True)
    primitive_source = Path(primitive_source).resolve(strict=True)
    document = json.loads(manifest.read_text())
    if document.get('schema_version') != 1 or not isinstance(document.get('bindings'), list):
        raise ValueError('invalid memory manifest schema')
    dependencies = {str(path): digest(path) for path in (manifest, primitive_source)}
    for binding in document['bindings']:
        filename = binding.get('init_file')
        if filename is None:
            continue
        if (not isinstance(filename, str) or not filename.endswith('.hex') or
                Path(filename).name != filename or '\\' in filename):
            raise ValueError('memory initializer requires a local image filename')
        path = (manifest.parent / filename).resolve(strict=True)
        if path.parent != manifest.parent:
            raise ValueError('memory initializer requires a local image file')
        identity = digest(path)
        if identity != binding.get('init_sha256'):
            raise ValueError('memory initializer identity mismatch')
        dependencies[str(path)] = identity
    missing = {'memory', 'translate', 'calyx'} - tools.keys()
    if missing:
        raise ValueError(f'missing compiler tools: {sorted(missing)}')
    if not calyx_library.is_absolute() or not calyx_library.is_dir():
        raise ValueError('SV compilation requires an absolute Calyx library directory')
    library_inputs = {str(path): digest(path) for path in sorted(calyx_library.rglob('*'))
                      if path.is_file()}
    if not library_inputs:
        raise ValueError('Calyx library is empty')
    stages = (
        (names[0], 'memory', ('--verify-each', '--mlir-print-debuginfo', '--emit-bytecode',
            '--llm2fpga-validate-memory-bindings=manifest=' + str(manifest) +
            ' manifest-sha256=' + dependencies[str(manifest)]), 'calyx.mlirbc'),
        (names[1], 'memory', ('--verify-each', '--mlir-print-debuginfo',
            '--emit-bytecode',
            '--llm2fpga-retire-memory-requests',
            '--llm2fpga-bind-model-memories=primitive-source=' + str(primitive_source) +
            ' primitive-source-sha256=' + dependencies[str(primitive_source)]) +
            (('--llm2fpga-expose-runtime-memories',) if runtime_interface else ()), 'calyx.mlirbc'),
    ) + ((
        (names[2], 'memory', ('--verify-each', '--emit-bytecode',
            '--llm2fpga-export-external-memory-interface=interface=interface.json'),
            'calyx.mlirbc'),
    ) if runtime_interface else ()) + (
        (names[3], 'translate', ('--export-calyx',), 'kernel.futil'),
        (names[4], 'calyx', ('-b', 'verilog', *CALYX_RESOURCE_PASSES,
                           *(('-p', 'external') if runtime_interface else ()),
                           '-l', str(calyx_library)), 'kernel.sv'),
    )
    source = Path(calyx_ir).resolve(strict=True)
    receipts = []
    for name, tool_name, arguments, filename in stages:
        tool = tools[tool_name]
        inputs = {**dependencies, str(source): digest(source)}
        if name == 'sv':
            inputs.update(library_inputs)
            if synthesis:
                # Pinned Calyx's native nested backend honors synthesis mode
                # for guard checks; its flat backend emits simulation tasks.
                arguments += ('--synthesis', '--nested')
        spec = StageSpec(name, (str(tool.executable), str(source), *arguments, '-o', filename),
                         inputs, tool.identities(),
                         {'profile': 'graph-initialized-memory-v1',
                          'runtime_interface': runtime_interface,
                          'profile_sha256': digest(Path(__file__))},
                         ((filename, 'interface.json') if name == 'external-interface' else (filename,)),
                         timeout_seconds)
        receipts.append(run_stage(spec, output_dir / name))
        source = output_dir / name / filename
    return CompilationResult('graph-initialized-memory-v1', source, tuple(receipts))


def export_memory_inventory(calyx_ir: Path, output_dir: Path, tool: CompilerTool,
                            *, timeout_seconds=120) -> tuple[Path, StageReceipt]:
    """Record native typed geometry/reference analysis as a compiler boundary."""
    source = Path(calyx_ir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    spec = StageSpec('memory-inventory',
        (str(tool.executable), str(source), '--verify-each', '--mlir-print-debuginfo',
         '--llm2fpga-export-memory-inventory=inventory=inventory.json',
         '--emit-bytecode', '-o', 'calyx.mlirbc'),
        {str(source): digest(source)}, tool.identities(),
        {'profile': 'graph-memory-inventory-v1', 'profile_sha256': digest(Path(__file__))},
        ('inventory.json', 'calyx.mlirbc'), timeout_seconds)
    return output_dir / 'inventory.json', run_stage(spec, output_dir)
