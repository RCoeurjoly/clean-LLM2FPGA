"""Derive exact value bounds from authenticated frozen tensor images.

This experimental metadata is insufficient to authorize a rewrite. A native
consumer must authenticate it and prove loads refer to immutable storage.
Storage layout, graph computation, and runtime input widths are unchanged.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .stages import _identity, _write_json, digest

COMPILER_ROOT = Path(__file__).resolve().parent


def build_frozen_range_certificate(manifest, images, output):
    manifest, output = Path(manifest), Path(output)
    image_paths = {name: Path(path) for name, path in images.items()}
    if output.is_symlink():
        raise ValueError('range certificate output must not be a symlink')
    output = output.resolve()
    protected = (manifest, *image_paths.values(), COMPILER_ROOT)
    if any(output == path.resolve() or output.is_relative_to(path.resolve()) or
           path.resolve().is_relative_to(output) for path in protected):
        raise ValueError('range certificate output overlaps input')
    output.unlink(missing_ok=True)
    if manifest.is_symlink() or any(path.is_symlink() for path in image_paths.values()):
        raise ValueError('range certificate inputs must not be symlinks')
    manifest = manifest.resolve(strict=True)
    raw_manifest = manifest.read_bytes()
    document = json.loads(raw_manifest)
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise ValueError('invalid graph input manifest')
    records = document.get('inputs')
    if not isinstance(records, list) or not records:
        raise ValueError('missing ordered graph inputs')
    frozen, targets = [], set()
    for index, record in enumerate(records):
        if (not isinstance(record, dict) or type(record.get('index')) is not int or
                record['index'] != index):
            raise ValueError('invalid graph input order')
        kind = record.get('kind')
        if kind == 'USER_INPUT':
            if record.get('sha256') is not None or record.get('target') in image_paths:
                raise ValueError('runtime input cannot supply frozen image bounds')
            continue
        if kind not in ('BUFFER', 'PARAMETER', 'CONSTANT_TENSOR'):
            raise ValueError('unsupported frozen input kind')
        target, identity = record.get('target'), record.get('sha256')
        if (not isinstance(target, str) or not target or target in targets or
                not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity)):
            raise ValueError('invalid frozen tensor identity')
        targets.add(target)
        frozen.append(record)
    if set(image_paths) != targets:
        raise ValueError('frozen image set differs from manifest')
    dependencies = {str(manifest): hashlib.sha256(raw_manifest).hexdigest(),
                    str(Path(__file__).resolve()): digest(Path(__file__))}
    tensors = []
    for record in frozen:
        width = {'torch.int32': 32, 'torch.int64': 64}.get(record.get('dtype'))
        shape = record.get('shape')
        if (width is None or not isinstance(shape, list) or
                any(type(size) is not int or size <= 0 for size in shape)):
            raise ValueError('invalid frozen tensor geometry')
        path = image_paths[record['target']].resolve(strict=True)
        data = path.read_bytes()
        words = data.splitlines()
        if (not data.endswith(b'\n') or len(words) != math.prod(shape) or
                any(not re.fullmatch(b'[0-9a-f]{' + str(width // 4).encode() + b'}', word)
                    for word in words)):
            raise ValueError('invalid frozen tensor image words')
        raw = bytearray()
        minimum, maximum = 1 << (width-1), -(1 << (width-1))
        for word in words:
            unsigned = int(word, 16)
            raw.extend(unsigned.to_bytes(width // 8, 'little'))
            value = unsigned if unsigned < (1 << (width-1)) else unsigned - (1 << width)
            minimum, maximum = min(minimum, value), max(maximum, value)
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('frozen image differs from tensor identity')
        extension = 'signed' if minimum < 0 else 'unsigned'
        value_width = width
        for bits in (8, 16, 32):
            if bits >= width:
                break
            lower = -(1 << (bits-1)) if extension == 'signed' else 0
            upper = (1 << (bits-1))-1 if extension == 'signed' else (1 << bits)-1
            if lower <= minimum and maximum <= upper:
                value_width = bits
                break
        image_hash = hashlib.sha256(data).hexdigest()
        dependencies[str(path)] = image_hash
        tensors.append({'target': record['target'], 'kind': record['kind'],
            'tensor_sha256': record['sha256'], 'image': str(path),
            'image_sha256': image_hash, 'words': len(words),
            'storage_width': width, 'value_width': value_width,
            'extension': extension, 'minimum': minimum, 'maximum': maximum})
    if any(digest(Path(path)) != expected for path, expected in dependencies.items()):
        raise ValueError('range certificate dependency changed during construction')
    body = {'schema_version': 1, 'tensors': tensors, 'dependency_hashes': dependencies,
            'readonly_proven': False,
            'scope': 'Exact frozen tensor value bounds only; native readonly/alias proof required'}
    result = {**body, 'identity_sha256': _identity(body)}
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, result)
    return result
