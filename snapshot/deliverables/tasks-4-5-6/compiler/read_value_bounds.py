"""Propose immutable read bounds without changing physical storage contracts.

The native binding pass must independently revalidate every image, disabled
write and reference contract before using these additional fields.
"""
from pathlib import Path
from .memory_plan import plan_memories
from .stages import digest


def plan_read_value_bounds(inventory, initializers, *, implementation='bram', offchip_immutable=False):
    result = plan_memories(inventory, initializers, implementation=implementation,
                           offchip_immutable=offchip_immutable)
    cells = {(m['component'], m['cell']): m for m in inventory['memories']}
    parent = {key: key for key in cells}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for edge in inventory['references']:
        source = (edge['caller'], edge['source'])
        target = (edge['callee'], edge['destination'])
        parent[root(target)] = root(source)
    groups = {}
    for key in cells:
        groups.setdefault(root(key), []).append(key)
    bindings = {(b['component'], b['cell']): b for b in result['bindings']}
    verified_images = {}
    for key, image in initializers.items():
        image = Path(image).resolve(strict=True)
        binding = bindings[key]
        if digest(image) != binding['init_sha256']:
            raise ValueError('initializer changed before range analysis')
        width = binding['width']
        low, high = None, None
        for row in image.read_bytes().splitlines():
            value = int(row, 16)
            if value >= (1 << (width-1)):
                value -= 1 << width
            low = value if low is None else min(low, value)
            high = value if high is None else max(high, value)
        if low is None or digest(image) != binding['init_sha256']:
            raise ValueError('initializer changed during range analysis')
        verified_images[key] = (low, high)
    for keys in groups.values():
        physical = [key for key in keys if cells[key]['physical']]
        if any(bindings[key]['writable'] for key in keys) or not all(key in verified_images for key in physical):
            continue
        low = min(verified_images[key][0] for key in physical)
        high = max(verified_images[key][1] for key in physical)
        signed = low < 0
        width = cells[physical[0]]['width']
        bits = width
        for candidate in (8, 16, 32):
            if candidate >= width:
                break
            minimum = -(1 << (candidate-1)) if signed else 0
            maximum = (1 << (candidate-1))-1 if signed else (1 << candidate)-1
            if minimum <= low and high <= maximum:
                bits = candidate
                break
        if bits < width:
            for key in keys:
                bindings[key]['value_width'] = bits
                bindings[key]['value_sign_extend'] = signed
    return result
