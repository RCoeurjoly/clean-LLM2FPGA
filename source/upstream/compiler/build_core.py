"""Common graph-to-core orchestration; numerical checks run downstream.

This driver authenticates metadata and calls compilers. It does not interpret
or rewrite serialized computation, and has no expected-answer input.
"""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from .build_profile import CoreBuildProfile
from .frontend.package_inputs import CompileInputs
from .generation_driver import ContextFunction, link_generation_functions, lower_generation_controller
from .graph_images import materialize_graph_images
from .ddr_images import materialize_ddr_images
from .memory_plan import initializers_from_origins, plan_memories
from .pipeline import compile_integer_graph, bind_initialized_memories, export_memory_inventory, integer_profile
from .stages import digest, _identity, _write_json, verify_stage_receipt
from scripts.pipeline.memory_policy import MemorySpec, emit_memory_sources


@dataclass(frozen=True)
class CoreReceipt:
    schema_version: int
    profile_sha256: str
    materialized_inputs_sha256: str
    output: str
    graphs: tuple[dict, ...]
    dependency_hashes: dict[str, str]
    identity_sha256: str
    scope: str = 'Compilation artifacts only; numerical and physical acceptance are downstream.'


def _load_export(directory, receipt):
    import torch
    from .frontend.capture import _hash_bytes, _tensor_identity
    path = directory / 'model.pt2'
    before = digest(path)
    exported = torch.export.load(path)
    if (digest(path) != before or
            _hash_bytes(exported.graph_module.code.encode()) != receipt.graph_sha256 or
            _hash_bytes(str(exported.graph_signature).encode()) != receipt.signature_sha256 or
            {name: _tensor_identity(tensor) for name, tensor in exported.state_dict.items()} != receipt.state_identities or
            {name: _tensor_identity(tensor) for name, tensor in exported.constants.items()} != receipt.constant_identities or
            {str(key): str(value) for key, value in exported.range_constraints.items()} != receipt.range_constraints):
        raise ValueError('captured graph or frozen inputs changed')
    return exported


def _owned_core_stage_dirs(output_dir: Path) -> tuple[Path, ...]:
    """Declare compiler namespaces without discovering files under the output."""
    names = ('graph-inputs', *(stage[0] for stage in integer_profile(
        entrypoint='main', pool_private_capacity=True, stable_scratch_pooling=True)))
    contexts = tuple(output_dir / 'contexts' / f'context-{length:02d}' / name
                     for length in range(4, 20) for name in names)
    controller = tuple(output_dir / 'controller' / name for name in names)
    initialized = tuple(output_dir / 'initialized' / name for name in
                        ('memory-validation', 'memory-binding', 'external-interface', 'futil', 'sv'))
    return (*contexts, *controller, output_dir / 'generation/linked',
            output_dir / 'inventory', *initialized)


def invalidate_core_receipts(output_dir: Path) -> None:
    """Remove prior success evidence while preserving artifacts and diagnostics."""
    output_dir = Path(output_dir).resolve()
    (output_dir / 'core.json').unlink(missing_ok=True)
    # Downstream wiring cannot retain acceptance when its source core rebuilds.
    for name in ('transport.json','domains/qualification.json',
                 'domains/elaboration/success.json','adapter/emission/success.json','native-core/success.json'):
        (output_dir / 'transport' / name).unlink(missing_ok=True)
    (output_dir / 'ddr/layout.json').unlink(missing_ok=True)
    for stage in _owned_core_stage_dirs(output_dir):
        (stage / 'success.json').unlink(missing_ok=True)


def build_core(inputs: CompileInputs, profile: CoreBuildProfile, output_dir: Path,
               *, timeout_seconds=120) -> CoreReceipt:
    """Build all sixteen graph specializations and their runtime controller.

    Every call starts a fresh compilation. Old stage receipts are invalidated
    before input checks; no cached intermediate can substitute for this build.
    Memory implementation is the profile's explicit policy, not an FPGA fit claim.
    """
    output_dir = Path(output_dir).resolve()
    invalidate_core_receipts(output_dir)
    profile.validate()
    if not isinstance(inputs, CompileInputs):
        raise ValueError('core requires authenticated CompileInputs')
    inputs.validate()
    if inputs.contract_sha256 != digest(profile.numerical_contract):
        raise ValueError('numerical contract differs from compile inputs')
    contract = json.loads(profile.numerical_contract.read_text())
    if inputs.input_hashes != contract['inputs'] or inputs.arithmetic != contract['arithmetic']:
        raise ValueError('numerical contract differs from compile inputs')
    if timeout_seconds <= 0:
        raise ValueError('positive compiler stage timeout is required')
    from .generation import capture_generation_contexts
    from .frontend.capture import import_raw_torch, serialize_raw_torch
    document = profile.document()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / 'profile.json', document)
    captures = capture_generation_contexts(inputs, profile.contexts, output_dir / 'captures')
    contexts, graphs, stage_directories = [], [], []
    images, frozen_identity = None, None
    for length, capture in zip(profile.contexts, captures, strict=True):
        directory = output_dir / 'captures' / f'context-{length:02d}'
        exported = _load_export(directory, capture)
        frozen = (capture.state_identities, capture.constant_identities)
        if frozen_identity is None:
            frozen_identity = frozen
            manifest = json.loads((directory / 'graph-inputs.json').read_text())
            images = materialize_graph_images(exported, manifest, output_dir)
        elif frozen != frozen_identity:
            raise ValueError('generation contexts have different frozen input contracts')
        raw = directory / 'raw.mlir'
        raw.write_text(serialize_raw_torch(import_raw_torch(exported)))
        core = output_dir / 'contexts' / f'context-{length:02d}'
        context_result = compile_integer_graph(raw, core, dict(profile.tools), target='buffers',
            graph_inputs=directory / 'graph-inputs.json', timeout_seconds=timeout_seconds,
            fuse_gemv_epilogues=profile.fuse_gemv_epilogues,
            pool_private_capacity=profile.pool_private_capacity)
        stage_directories.extend(core / receipt.name for receipt in context_result.receipts)
        contexts.append(ContextFunction(length, core / 'buffers'))
        graphs.append({'context': length, 'program_sha256': capture.program_sha256,
                       'archive_sha256': digest(directory / 'model.pt2')})
    linked = link_generation_functions(tuple(contexts), output_dir / 'generation',
        profile.tools['torch'], timeout_seconds=timeout_seconds)
    stage_directories.append(linked.output.parent)
    controller = lower_generation_controller(linked.output.parent, output_dir / 'controller',
        dict(profile.tools), timeout_seconds=timeout_seconds,
        recover_restoring_sqrt_loops=profile.recover_restoring_sqrt_loops,
        coalesce_pointwise_pingpong=profile.coalesce_pointwise_pingpong,
        fuse_gemv_epilogues=profile.fuse_gemv_epilogues,
        pool_private_capacity=profile.pool_private_capacity,
        stable_scratch_pooling=profile.stable_scratch_pooling)
    stage_directories.extend(output_dir / 'controller' / receipt.name
                             for receipt in controller.receipts)
    inventory_path, _ = export_memory_inventory(controller.output, output_dir / 'inventory',
        profile.tools['memory'], timeout_seconds=timeout_seconds)
    stage_directories.append(inventory_path.parent)
    inventory = json.loads(inventory_path.read_text())
    bindings = plan_memories(inventory, initializers_from_origins(inventory, images),
        implementation=profile.memory_implementation, narrow_immutable=profile.narrow_immutable,
        offchip_immutable=profile.offchip_immutable)
    manifest_path = output_dir / 'bindings.json'
    _write_json(manifest_path, bindings)
    ddr_layout = None
    if profile.offchip_immutable:
        ddr_layout = materialize_ddr_images(manifest_path, output_dir / 'ddr',
            **document['memory_policy']['ddr_layout'])
    first = json.loads((output_dir / 'captures/context-04/graph-inputs.json').read_text())
    specs = [MemorySpec('buffer_' + str(record['index']),
        64 if record['dtype'] == 'torch.int64' else 32, math.prod(record['shape']),
        profile.memory_implementation, 1 if profile.memory_implementation == 'register' else 2,
        False, images[record['target']].name)
        for record in first['inputs'] if record['kind'] != 'USER_INPUT']
    declarations = emit_memory_sources(specs, include_narrow_rom=profile.narrow_immutable)
    primitive = output_dir / 'closure_memory.sv'
    primitive.write_text(declarations['sv'])
    initialized = bind_initialized_memories(controller.output, manifest_path, primitive,
        output_dir / 'initialized', dict(profile.tools), calyx_library=profile.calyx_library,
        runtime_interface=True,
        timeout_seconds=timeout_seconds)
    stage_directories.extend(output_dir / 'initialized' / receipt.name
                             for receipt in initialized.receipts)
    inputs.validate()
    profile.validate()
    dependencies = dict(document['dependency_hashes'])
    for identities in document['tool_hashes'].values():
        dependencies.update(identities)
    # Only declared artifacts belong to compilation. Unrelated files in the
    # output directory, including expected-answer files, are never opened.
    artifacts = {output_dir / 'profile.json', manifest_path, primitive,
                 output_dir / 'generation/empty.mlir', *images.values()}
    if ddr_layout is not None:
        artifacts.update(output_dir / 'ddr' / name for name in ('layout.json', 'images.bin'))
        dependencies.update(ddr_layout['dependency_hashes'])
    for length in profile.contexts:
        directory = output_dir / 'captures' / f'context-{length:02d}'
        artifacts.update(directory / name for name in (
            'model.pt2', 'capture.json', 'graph-inputs.json', 'graph.py', 'raw.mlir'))
    for stage in stage_directories:
        receipt_path, spec_path = stage / 'success.json', stage / 'spec.json'
        dependencies.update(verify_stage_receipt(stage))
        spec = json.loads(spec_path.read_text())
        artifacts.update((receipt_path, spec_path))
        artifacts.update(receipt_path.parent / name for name in spec['outputs'])
        dependencies.update(spec['input_hashes'])
        dependencies.update(spec['tool_hashes'])
    dependencies.update({str(path): digest(path) for path in artifacts})
    body = {'schema_version': 1, 'profile_sha256': profile.identity_sha256,
            'materialized_inputs_sha256': inputs.materialized_sha256,
            'output': str(initialized.output), 'graphs': tuple(graphs),
            'dependency_hashes': dependencies}
    receipt = CoreReceipt(**body, identity_sha256=_identity(body))
    _write_json(output_dir / 'core.json', asdict(receipt))
    return receipt


def verify_core_receipt(output_dir: Path, profile: CoreBuildProfile) -> dict:
    """Authenticate compilation artifacts; this does not establish RTL accuracy."""
    profile.validate()
    receipt = json.loads((Path(output_dir) / 'core.json').read_text())
    body = {key: receipt[key] for key in ('schema_version', 'profile_sha256',
        'materialized_inputs_sha256', 'output', 'graphs', 'dependency_hashes')}
    if (receipt['schema_version'] != 1 or receipt['profile_sha256'] != profile.identity_sha256
            or receipt['identity_sha256'] != _identity(body)
            or [record['context'] for record in receipt['graphs']] != list(range(4, 20))
            or receipt['output'] not in receipt['dependency_hashes']):
        raise ValueError('invalid core receipt')
    for name, expected in receipt['dependency_hashes'].items():
        path = Path(name)
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or digest(path) != expected:
            raise ValueError('core dependency changed or missing')
    final_stage = Path(receipt['output']).parent
    if any(str(final_stage / name) not in receipt['dependency_hashes']
           for name in ('spec.json', 'success.json')):
        raise ValueError('core final stage evidence missing')
    for name in receipt['dependency_hashes']:
        if Path(name).name == 'success.json':
            stage_dependencies = verify_stage_receipt(Path(name).parent)
            if any(receipt['dependency_hashes'].get(path) != identity
                   for path, identity in stage_dependencies.items()):
                raise ValueError('core stage dependency missing or inconsistent')
    return receipt
