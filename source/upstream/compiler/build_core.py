"""Compile authenticated model inputs to controller loops and frozen images."""
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from .build_profile import CoreBuildProfile
from .frontend.package_inputs import CompileInputs
from .generation_driver import ContextFunction, link_generation_functions, lower_generation_controller
from .graph_images import materialize_graph_images
from .pipeline import compile_integer_graph
from .stages import digest, verify_stage_receipt


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


def _compile_core(inputs: CompileInputs, profile: CoreBuildProfile, output_dir: Path,
                  *, timeout_seconds=120) -> tuple[Path, tuple[Path, ...]]:
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
    output_dir.mkdir(parents=True, exist_ok=True)
    captures = capture_generation_contexts(inputs, profile.contexts, output_dir / 'captures')
    contexts, stage_directories = [], []
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
    inputs.validate()
    profile.validate()
    for stage in stage_directories:
        verify_stage_receipt(stage)
    return controller.output, tuple(images.values())


def build_core(inputs: CompileInputs, profile: CoreBuildProfile, output_dir: Path,
               *, timeout_seconds=120) -> Path:
    """Publish only checked controller loops and frozen input memory images.

    Intermediate captures and IR remain in a temporary build workspace. A failed
    compilation leaves no output directory that could be mistaken for a core.
    """
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError(f'core output already exists: {output_dir}')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='graph-core-') as temporary:
        work = Path(temporary)
        loops, images = _compile_core(inputs, profile, work / 'build',
                                      timeout_seconds=timeout_seconds)
        output_dir.mkdir()
        try:
            destination = output_dir / 'controller/loops/loops.mlir'
            destination.parent.mkdir(parents=True)
            shutil.copyfile(loops, destination)
            for source in images:
                shutil.copyfile(source, output_dir / source.name)
        except BaseException:
            shutil.rmtree(output_dir)
            raise
    return output_dir / 'controller/loops/loops.mlir'
