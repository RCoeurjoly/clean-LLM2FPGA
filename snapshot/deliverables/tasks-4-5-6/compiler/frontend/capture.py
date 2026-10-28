"""Capture graph computation and import raw Torch IR before legalization."""
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import torch


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _hash_json(value):
    return _hash_bytes(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def _tensor_identity(tensor):
    tensor = tensor.detach().cpu().contiguous()
    return {'shape': list(tensor.shape), 'dtype': str(tensor.dtype),
            'sha256': _hash_bytes(tensor.reshape(-1).view(torch.uint8).numpy().tobytes())}


@dataclass(frozen=True)
class CaptureReceipt:
    schema_version: int
    program_sha256: str
    graph_sha256: str
    signature_sha256: str
    state_identities: dict
    constant_identities: dict
    user_input_count: int
    inputs: list
    operators: dict
    nodes: list
    range_constraints: dict
    support_sha256: str


def capture_model(model: torch.nn.Module, example_inputs: tuple,
                  output_dir: Path) -> CaptureReceipt:
    support_bytes = Path(__file__).with_name('operator_support.json').read_bytes()
    support = json.loads(support_bytes)['operators']
    exported = torch.export.export(model, example_inputs, strict=True)
    nodes = []
    counts = Counter()
    for node in exported.graph.nodes:
        if node.op != 'call_function':
            continue
        operator = str(node.target)
        value = node.meta.get('val')
        location = node.meta.get('stack_trace', '')
        record = {'name': node.name, 'operator': operator, 'location': location,
                  'dtype': str(getattr(value, 'dtype', None)),
                  'shape': list(getattr(value, 'shape', ())) }
        if operator not in support:
            raise ValueError(f"capture: unsupported {operator}, node={node.name}, "
                             f"dtype={record['dtype']}, shape={record['shape']}, location={location}")
        nodes.append(record)
        counts[operator] += 1
    user_inputs = [spec for spec in exported.graph_signature.input_specs
                   if spec.kind.name == 'USER_INPUT']
    if len(user_inputs) != len(example_inputs):
        raise ValueError('capture: user inputs were replaced by constants')
    if any(spec.arg.__class__.__name__ != 'TensorArgument' for spec in user_inputs):
        raise ValueError('capture: all runtime inputs must be tensors')
    state = {name: _tensor_identity(tensor) for name, tensor in sorted(exported.state_dict.items())}
    constants = {}
    for name, value in sorted(exported.constants.items()):
        if not isinstance(value, torch.Tensor):
            raise ValueError(f'capture: unsupported non-tensor constant {name}')
        constants[name] = _tensor_identity(value)
    placeholders = [node for node in exported.graph.nodes if node.op == 'placeholder']
    if len(placeholders) != len(exported.graph_signature.input_specs):
        raise ValueError('capture: graph input signature/placeholder count mismatch')
    graph_inputs = []
    for index, (spec, node) in enumerate(zip(exported.graph_signature.input_specs,
                                           placeholders, strict=True)):
        if spec.arg.__class__.__name__ != 'TensorArgument':
            raise ValueError('capture: all imported graph inputs must be tensors')
        kind = spec.kind.name
        if kind not in ('PARAMETER', 'BUFFER', 'CONSTANT_TENSOR', 'USER_INPUT'):
            raise ValueError(f'capture: unsupported graph input kind {kind}')
        value = node.meta.get('val')
        identity = state.get(spec.target) if kind in ('PARAMETER', 'BUFFER') else (
            constants.get(spec.target) if kind == 'CONSTANT_TENSOR' else None)
        if kind != 'USER_INPUT' and identity is None:
            raise ValueError('capture: graph input has no declared frozen tensor identity')
        graph_inputs.append({'index': index, 'name': spec.arg.name, 'kind': kind,
            'target': spec.target, 'shape': list(value.shape), 'dtype': str(value.dtype),
            'sha256': identity['sha256'] if identity else None})
    graph_hash = _hash_bytes(exported.graph_module.code.encode())
    signature_hash = _hash_bytes(str(exported.graph_signature).encode())
    constraints = {str(key): str(value) for key, value in exported.range_constraints.items()}
    inputs = [{'name': spec.arg.name, 'shape': list(value.shape), 'dtype': str(value.dtype)}
              for spec, value in zip(user_inputs, example_inputs, strict=True)]
    identity = {'graph': graph_hash, 'signature': signature_hash, 'state': state,
                'constants': constants, 'constraints': constraints, 'inputs': inputs}
    receipt = CaptureReceipt(1, _hash_json(identity), graph_hash, signature_hash,
                             state, constants, len(user_inputs), inputs, dict(sorted(counts.items())),
                             nodes, constraints, _hash_bytes(support_bytes))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Serialization, not a text transformation of the computation.
    torch.export.save(exported, output_dir / 'model.pt2')
    (output_dir / 'graph-inputs.json').write_text(json.dumps({
        'schema_version': 1, 'signature_sha256': signature_hash,
        'inputs': graph_inputs}, sort_keys=True, indent=2) + '\n')
    (output_dir / 'graph.py').write_text(exported.graph_module.code)
    (output_dir / 'capture.json').write_text(json.dumps(asdict(receipt), indent=2) + '\n')
    return receipt


def import_raw_torch(exported: torch.export.ExportedProgram):
    from torch_mlir import fx
    module = fx.export_and_import(exported, output_type='raw')
    module.operation.verify()
    return module


def serialize_raw_torch(module) -> str:
    """Preserve importer source locations using MLIR's native serializer."""
    module.operation.verify()
    return module.operation.get_asm(enable_debug_info=True)
