"""Downstream review metadata checks; never a compilation dependency.

This module cannot grant approval. It checks an existing user decision against
its prepared candidate and declared file identities. Complete dependency
enumeration and numerical/physical readiness are separate review-index gates.
"""
from dataclasses import asdict
import json
from pathlib import Path
from .stages import _identity, digest, verify_stage_receipt


def compiler_source_inventory(root: Path) -> dict:
    """Enumerate authored tool sources for downstream manual review.

    This deliberately includes the full native source and graph-tool recipe
    directories, including diagnostic helpers. Binary identities alone do not
    identify the source a reviewer must read. Runtime orchestration, numerical
    contracts, model inputs and external pinned upstream sources must also be
    enumerated by the composed review index; this inventory grants no readiness
    or approval and is never a compilation dependency.
    """
    root = Path(root).resolve(strict=True)
    paths = {root / 'flake.nix', root / 'flake.lock',
             root / 'tests/yosys_gzip_large_write.cpp'}
    for name in ('compiler/passes', 'compiler/native'):
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f'missing native compiler source directory: {name}')
        for path in directory.rglob('*'):
            if path.is_symlink():
                raise ValueError(f'symlink in compiler source inventory: {path}')
            if path.is_file():
                paths.add(path)
    paths.update((root / 'nix').glob('graph-*.nix'))
    paths.update((root / 'nix/patches').glob('graph-*.patch'))
    identities = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'missing compiler source dependency: {path}')
        identities[str(path)] = digest(path)
    body = {'schema_version': 1, 'root': str(root),
            'scope': 'authored compiler tool sources; composed review coverage remains required',
            'dependency_hashes': identities}
    return {**body, 'identity_sha256': _identity(body)}


def upstream_patch_inventory(root: Path, source_roots: dict[str, Path]) -> dict:
    """Pair authored patches with original files, without applying patches.

    Source roots must come from the pinned build definitions. This is coverage
    of patched upstream sites, not proof of compiled tool identity or coverage
    of every unmodified upstream pass. Patch order belongs to the build recipes.
    """
    roles = {'calyx', 'circt', 'torch_mlir', 'vast', 'iverilog'}
    if not roles.issubset(source_roots) or not set(source_roots).issubset(roles | {'yosys'}):
        raise ValueError('upstream source root roles are incomplete')
    roots = {}
    for role, name in source_roots.items():
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError(f'invalid upstream source root: {role}')
        roots[role] = path.resolve(strict=True)
    patches, dependencies = [], {}
    for patch in sorted((Path(root) / 'nix/patches').glob('graph-*.patch')):
        if patch.is_symlink() or not patch.is_file():
            raise ValueError(f'invalid upstream patch source: {patch}')
        originals = []
        targets = [line[4:].split('\t', 1)[0] for line in patch.read_text().splitlines()
                   if line.startswith('--- ')]
        if not targets:
            raise ValueError(f'patch target is missing: {patch}')
        for target in targets:
            if not target.startswith('a/'):
                raise ValueError(f'unsupported patch target: {target}')
            relative = Path(target[2:])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f'escaping patch target: {target}')
            name = str(relative)
            if (name.startswith('calyx/') or name == 'src/main.rs'
                    or name == 'primitives/binary_operators.futil'):
                role = 'calyx'
            elif name == 'SCFToCalyx.cpp':
                role, relative = 'circt', Path('lib/Conversion/SCFToCalyx/SCFToCalyx.cpp')
            elif name == 'CalyxEmitter.cpp':
                role, relative = 'circt', Path('lib/Dialect/Calyx/Export/CalyxEmitter.cpp')
            elif name == 'FlattenMemRefs.cpp':
                role, relative = 'circt', Path('lib/Transforms/FlattenMemRefs.cpp')
            elif name.startswith(('include/circt/', 'lib/Dialect/Calyx/')):
                role = 'circt'
            elif name in ('CMakeLists.txt', 'tools/torch-mlir-opt/CMakeLists.txt'):
                role = 'torch_mlir'
            elif name.startswith('src/subset/'):
                role = 'vast'
            elif name.startswith('vvp/'):
                role = 'iverilog'
            elif name in ('kernel/gzip.h', 'kernel/gzip.cc', 'kernel/consteval.h',
                          'passes/techmap/deminout.cc',
                          'passes/proc/proc_prune.cc',
                          'passes/opt/opt_reduce.cc',
                          'passes/fsm/fsm_opt.cc',
                          'passes/hierarchy/flatten.cc'):
                role = 'yosys'
            elif name.startswith('techlibs/xilinx/'):
                # These native mapping sources are installed unchanged in the
                # pinned upstream Yosys data tree before our tool-source patch.
                role = 'yosys'
                if 'yosys' in roots and (roots['yosys'] / relative).is_file():
                    pass  # Full pinned compiler source tree.
                else:
                    relative = Path(name.removeprefix('techlibs/'))
            else:
                raise ValueError(f'unsupported patch target: {target}')
            if role not in roots:
                raise ValueError(f'upstream source root missing: {role}')
            source = roots[role] / relative
            if (source.is_symlink() or not source.is_file()
                    or not source.resolve().is_relative_to(roots[role])):
                raise ValueError(f'original upstream source missing or escaping: {source}')
            originals.append(str(source))
            dependencies[str(source)] = digest(source)
        dependencies[str(patch.resolve())] = digest(patch)
        patches.append({'patch': str(patch.resolve()), 'original_sources': originals})
    body = {'schema_version': 1, 'kind': 'upstream-patch-source-inventory',
            'source_roots': {role: str(path) for role, path in sorted(roots.items())},
            'patches': patches, 'dependency_hashes': dict(sorted(dependencies.items())),
            'scope': 'patched upstream sites only; build order and unmodified upstream coverage remain separate'}
    return {**body, 'identity_sha256': _identity(body)}


def build_review_index(core, evidence: tuple[Path, ...]) -> dict:
    """Prepare authenticated coverage, never infer correctness from receipts.

    Compilation success receipts authenticate declared stages. They do not
    establish model semantics, physical implementation or human approval.
    This preparation index leaves every chunk open until those separate gates
    are implemented and verified against the frozen candidate.
    """
    from .build_core import CoreReceipt
    if not isinstance(core, CoreReceipt) or type(evidence) is not tuple:
        raise ValueError('review index requires CoreReceipt and evidence tuple')
    body = {key: asdict(core)[key] for key in ('schema_version', 'profile_sha256',
        'materialized_inputs_sha256', 'output', 'graphs', 'dependency_hashes')}
    if (core.schema_version != 1 or core.identity_sha256 != _identity(body)
            or [graph.get('context') for graph in core.graphs] != list(range(4, 20))
            or core.output not in core.dependency_hashes):
        raise ValueError('invalid core review receipt')
    for name, expected in core.dependency_hashes.items():
        path = Path(name)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or digest(path) != expected):
            raise ValueError(f'core review dependency changed or missing: {name}')
    final_stage = Path(core.output).parent
    if any(str(final_stage / name) not in core.dependency_hashes
           for name in ('spec.json', 'success.json')):
        raise ValueError('core review final stage evidence missing')
    for name in core.dependency_hashes:
        if Path(name).name == 'success.json':
            identities = verify_stage_receipt(Path(name).parent)
            if any(core.dependency_hashes.get(path) != identity
                   for path, identity in identities.items()):
                raise ValueError('core review stage dependency missing or inconsistent')

    root = Path(__file__).resolve().parents[1]
    inventory = compiler_source_inventory(root)
    dependencies = {**core.dependency_hashes, **inventory['dependency_hashes']}
    compiled_source_origins = {}
    # Nix receipts name immutable compilation sources. Read local counterparts
    # only when their contents equal those authenticated original sources.
    for name in core.dependency_hashes:
        if Path(name).name != 'profile.json':
            continue
        profile = json.loads(Path(name).read_text())
        if _identity(profile) != core.profile_sha256:
            raise ValueError('compiled source profile identity mismatch')
        sources = profile['dependency_hashes']
        source_roots = {Path(path).parents[2] for path in sources
                        if Path(path).parts[-3:] ==
                        ('compiler', 'frontend', 'dependencies.json')}
        for source_root in source_roots:
            for original, expected in sources.items():
                path = Path(original)
                if not path.is_relative_to(source_root):
                    continue
                local = root / path.relative_to(source_root)
                if (core.dependency_hashes.get(original) != expected
                        or local.is_symlink() or not local.is_file()
                        or digest(local) != expected):
                    raise ValueError(f'compiled source differs from review candidate: {original}')
                dependencies[str(local)] = expected
                compiled_source_origins[str(local)] = original
    dependencies[str(Path(__file__).resolve())] = digest(Path(__file__).resolve())
    # Downstream synthesis orchestration is outside the compilation-only
    # bundle, but is an active production dependency the user must read.
    for name in ('export_graph_core_synthesis.py', 'map_graph_core_xc7.py',
                 'export_graph_ddr_transport.py'):
        synthesis_driver = root / 'scripts/pipeline' / name
        if synthesis_driver.is_symlink() or not synthesis_driver.is_file():
            raise ValueError('invalid production synthesis source dependency')
        dependencies[str(synthesis_driver)] = digest(synthesis_driver)
    for name in ('board_pins.py', 'litedram_export.py', 'yosys_pipeline.py', 'frozen_ranges.py', 'read_value_bounds.py'):
        board_source = root / 'compiler' / name
        if board_source.is_symlink() or not board_source.is_file():
            raise ValueError('invalid production board/synthesis source dependency')
        dependencies[str(board_source)] = digest(board_source)
    for name in ('matmul.py', 'provenance.json'):
        task2_source = root / 'model_inputs/task2' / name
        if task2_source.is_symlink() or not task2_source.is_file():
            raise ValueError('invalid original Task 2 source dependency')
        dependencies[str(task2_source)] = digest(task2_source)
    # Verification code is downstream of compilation, but belongs to manual
    # review: result fixtures cannot substitute for reading their verifier.
    comparison = root / 'scripts/comparison'
    verification_sources = set(comparison.glob('graph_*.py'))
    verification_sources.add(comparison / 'graph_generation_progress_main.cpp')
    verification_sources.update(comparison.glob('verify_graph_*.py'))
    verification_sources.update(comparison.glob('build_graph_*.py'))
    verification_sources.add(comparison / 'verify_closure_reference.py')
    for path in sorted(verification_sources):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'invalid verification source dependency: {path}')
        dependencies[str(path)] = digest(path)
    evidence_hashes = {}
    for name in evidence:
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError(f'invalid review evidence file: {name}')
        evidence_hashes[str(path)] = digest(path)
    contracts = {str(path): digest(path) for path in sorted(
        (root / 'compiler/contracts').glob('*.json'))}
    for name in ('compiler/frontend/operator_support.json',
                 'compiler/frontend/dependencies.json',
                 'calyx/primitives/closure_memory.futil'):
        path = root / name
        contracts[str(path)] = digest(path)
    dependencies.update(contracts)
    dependencies.update(evidence_hashes)

    titles = (
        'PyTorch capture, operator support and graph provenance',
        'Fixed-point representation and package/reference boundary',
        'IR parsing, serialization and validation',
        'Shape, stride, address and control lowering',
        'Sharing, aliasing, narrowing and lifetime invariants',
        'Memory selection, initialization and completion protocol',
        'Pass manager, common build profile and stage provenance',
        'Calyx to SystemVerilog and upstream compatibility',
        'Board wrapper and core interface',
        'Synthesis, placement, routing and bitstream drivers',
        'Composed pipeline, numerical checks and evidence gates',
    )
    owned = [[] for _ in titles]
    for name in sorted(dependencies):
        path = Path(name)
        if not path.is_relative_to(root):
            continue
        relative = str(path.relative_to(root))
        if relative.startswith(('compiler/frontend/capture', 'model_inputs/task2/')):
            chunk = 0
        elif relative.startswith(('compiler/frontend/', 'TinyStories/serial_gemv')):
            chunk = 1
        elif (relative.startswith(('compiler/native/', 'nix/patches/'))
              or relative == 'tests/yosys_gzip_large_write.cpp'):
            chunk = 7
        elif relative == 'compiler/passes/torch/TypedTensor.h':
            chunk = 2
        elif relative in (
                'compiler/passes/torch/BufferReuse.cpp',
                'compiler/passes/torch/FunctionSharing.cpp',
                'compiler/passes/calyx/ExclusiveCombInputs.cpp',
                'compiler/passes/calyx/IdenticalComb.cpp',
                'compiler/passes/calyx/TransactionalDividers.cpp',
                'compiler/passes/calyx/RegisterBoundedComb.cpp',
                'compiler/passes/calyx/StaticLoopCounters.cpp',
                'compiler/passes/torch/ProducerSlices.cpp',
                'compiler/passes/torch/PointwiseFusion.cpp',
                'compiler/passes/torch/HorizontalPointwiseFusion.cpp'):
            chunk = 4
        elif relative in ('compiler/passes/calyx/RuntimeInterface.cpp',
                          'compiler/board_pins.py', 'compiler/litedram_export.py'):
            chunk = 8
        elif relative in (
                'compiler/passes/calyx/MemoryBindings.cpp',
                'compiler/passes/calyx/MemoryContracts.cpp',
                'compiler/passes/calyx/MemoryInventory.cpp',
                'compiler/passes/calyx/MemoryRetirement.cpp',
                'compiler/passes/calyx/RuntimeMemories.cpp'):
            chunk = 5
        elif relative.startswith('compiler/passes/'):
            chunk = 3
        elif relative.startswith(('rtl/primitives/', 'calyx/primitives/',
                                  'compiler/graph_images', 'compiler/memory_plan',
                                  'compiler/frozen_ranges',
                                  'compiler/read_value_bounds',
                                  'scripts/pipeline/memory_policy')):
            chunk = 5
        elif relative == 'compiler/yosys_pipeline.py':
            chunk = 9
        elif relative.startswith(('compiler/review', 'compiler/contracts/',
                                  'scripts/comparison/')):
            chunk = 10
        else:
            chunk = 6
        owned[chunk].append(name)
    chunks = []
    for number, title in enumerate(titles):
        chunks.append({'chunk_id': f'{number + 1:02d}', 'title': title,
            'reading_order': owned[number],
            'files': {name: dependencies[name] for name in owned[number]},
            'contracts': contracts, 'dependencies': dependencies,
            'evidence': evidence_hashes, 'prepared_commit': None,
            'state': 'preparation-open', 'user_decision': None,
            'approved_commit': None})
    result = {'schema_version': 1, 'core_identity_sha256': core.identity_sha256,
        'native_source_inventory_sha256': inventory['identity_sha256'],
        'compiled_source_origins': dict(sorted(compiled_source_origins.items())),
        'dependency_hashes': dict(sorted(dependencies.items())), 'chunks': chunks,
        'ready_for_manual_review': False,
        'limitations': [
            'Declared compilation receipts do not prove complete stage coverage or RTL accuracy.',
            'External pinned upstream source reading remains to be inventoried.',
            'Board and physical-driver production integration coverage remains open.',
            'Per-chunk contracts, invariants and validation remain to be finalized.',
            'The candidate is not frozen; no code approval is recorded.',
        ]}
    return {**result, 'identity_sha256': _identity(result)}


def validate_chunk_approval(record: dict) -> dict:
    """Return effective approval state without changing the recorded decision."""
    try:
        body = {key: record[key] for key in (
            'chunk_id', 'prepared_commit', 'files', 'contracts', 'dependencies')}
        if (not isinstance(body['chunk_id'], str) or not body['chunk_id']
                or not isinstance(body['prepared_commit'], str) or not body['prepared_commit']
                or record['identity_sha256'] != _identity(body)):
            raise ValueError('invalid chunk candidate identity')
        changed = []
        for role in ('files', 'contracts', 'dependencies'):
            identities = body[role]
            if not isinstance(identities, dict) or not identities:
                raise ValueError(f'missing chunk {role}')
            for name, expected in identities.items():
                path = Path(name)
                if (not path.is_absolute() or not isinstance(expected, str)
                        or len(expected) != 64
                        or any(char not in '0123456789abcdef' for char in expected)):
                    raise ValueError('invalid chunk file identity')
                try:
                    current = not path.is_symlink() and path.is_file() and digest(path) == expected
                except OSError:
                    current = False
                if not current:
                    changed.append(name)
        decision = record.get('user_decision')
        current = (decision == 'approved' and not changed
                   and record.get('approved_commit') == body['prepared_commit'])
        result = dict(record)
        result['approval_current'] = current
        result['changed_files'] = sorted(set(changed))
        if decision == 'approved':
            result['state'] = 'approved' if current else 'reopened'
        elif record.get('state') == 'approved':
            raise ValueError('chunk approval requires a recorded user decision')
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f'invalid chunk approval record: {error}') from error
