"""Serialize authenticated frozen graph tensors as memory words before RTL."""
import hashlib
from pathlib import Path


def materialize_graph_images(exported, manifest: dict, output_dir: Path) -> dict[str, Path]:
    import torch
    if manifest.get('schema_version') != 1:
        raise ValueError('invalid graph input manifest')
    records = manifest['inputs']
    specs = exported.graph_signature.input_specs
    if len(records) != len(specs):
        raise ValueError('graph input signature mismatch')
    prepared = []
    targets = set()
    for index, (record, spec) in enumerate(zip(records, specs, strict=True)):
        kind = spec.kind.name
        if record['index'] != index or record['kind'] != kind or record['target'] != spec.target:
            raise ValueError('graph input signature mismatch')
        if kind == 'USER_INPUT':
            if record['sha256'] is not None:
                raise ValueError('runtime input cannot be an initialization image')
            continue
        if kind not in ('BUFFER', 'PARAMETER', 'CONSTANT_TENSOR'):
            raise ValueError('unsupported frozen graph input kind')
        target = spec.target
        if target in targets:
            raise ValueError('duplicate frozen graph target')
        targets.add(target)
        tensor = (exported.constants if kind == 'CONSTANT_TENSOR' else exported.state_dict)[target]
        tensor = tensor.detach().cpu().contiguous()
        if tensor.dtype not in (torch.int32, torch.int64):
            raise ValueError('initialization supports signed int32/int64 graph tensors')
        if list(tensor.shape) != record['shape'] or str(tensor.dtype) != record['dtype']:
            raise ValueError('frozen graph tensor geometry mismatch')
        identity = hashlib.sha256(tensor.reshape(-1).view(torch.uint8).numpy().tobytes()).hexdigest()
        if identity != record['sha256']:
            raise ValueError('frozen graph tensor identity mismatch')
        width = tensor.element_size() * 8
        words = ''.join(f'{int(value) % (1 << width):0{width // 4}x}\n'
                        for value in tensor.reshape(-1).tolist())
        prepared.append((target, f'input-{index:04d}.hex', words))
    # No image is emitted until every frozen input has been authenticated.
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for target, filename, words in prepared:
        path = output_dir / filename
        path.write_text(words)
        result[target] = path
    return result
