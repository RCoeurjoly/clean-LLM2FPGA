"""Derive complete memory contracts from native inventory and initializer inputs.

No emitted computation is read as text. Cell symbols are inventory identifiers;
none are matched against model-specific names. Frozen graph buffers supply the
initializer mapping; resolving their graph provenance is a separate boundary.
"""
import hashlib
from pathlib import Path
from scripts.pipeline.memory_policy import MemorySpec, validate_image


def plan_memories(inventory: dict, initializers: dict[tuple[str, str], Path], *,
                  implementation='bram', narrow_immutable=False, offchip_immutable=False,
                  lut_memory_max_bits=0) -> dict:
    """Optionally compact immutable images across each reference contract group.

    Off-chip selection applies to entire immutable reference groups; runtime and
    scratch memories keep the selected on-chip implementation. DDR reads complete
    dynamically, represented by read_latency=0. Storage narrowing is unqualified
    for this policy and rejected.

    Ports retain their declared width. The native binding pass independently
    verifies every image word against the proposed sign/zero extension.
    A positive LUT threshold changes placement of whole on-chip reference
    groups based on their certified capacity. Both placements complete in two
    cycles; writable storage retains its full logical word width.
    """
    if type(lut_memory_max_bits) is not int or lut_memory_max_bits < 0:
        raise ValueError('LUT memory threshold must be a nonnegative integer')
    if lut_memory_max_bits and implementation != 'bram':
        raise ValueError('LUT memory threshold requires a BRAM base implementation')
    if offchip_immutable and narrow_immutable:
        raise ValueError('off-chip storage narrowing is not qualified')
    if inventory.get('schema_version') != 1:
        raise ValueError('invalid memory inventory schema')
    if implementation not in ('register', 'bram', 'lutram'):
        raise ValueError('unsupported memory implementation')
    cells = {}
    for memory in inventory['memories']:
        key = (memory['component'], memory['cell'])
        if key in cells:
            raise ValueError('duplicate memory inventory cell')
        cells[key] = memory
    parents = {key: key for key in cells}
    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key
    targets = set()
    for edge in inventory['references']:
        source = (edge['caller'], edge['source'])
        target = (edge['callee'], edge['destination'])
        if source not in cells or target not in cells:
            raise ValueError('reference names an absent memory')
        if cells[target]['physical']:
            raise ValueError('reference targets a physical memory')
        if any(cells[source][field] != cells[target][field]
               for field in ('width', 'depth', 'address_width')):
            raise ValueError('reference geometry mismatch')
        parents[root(target)] = root(source)
        targets.add(target)
    if any(not memory['physical'] and key not in targets for key, memory in cells.items()):
        raise ValueError('unbound reference memory')
    for key in initializers:
        if key not in cells or not cells[key]['physical']:
            raise ValueError('initializer must name a physical inventory memory')
    groups = {}
    for key in cells:
        groups.setdefault(root(key), []).append(key)
    mutable = {}
    for representative, keys in groups.items():
        physical = [key for key in keys if cells[key]['physical']]
        if not physical:
            raise ValueError('reference group has no physical memory')
        initialized = [key in initializers for key in physical]
        if any(initialized) and not all(initialized):
            raise ValueError('mixed initializer contracts across caller references')
        writes = any(not cells[key]['writes_disabled'] for key in keys)
        if writes and any(key in initializers for key in keys):
            raise ValueError('immutable initializer has a connected memory write')
        mutable[representative] = writes or any(key not in initializers for key in physical)
    images, image_hashes, bounds = {}, {}, {}
    for key, path in initializers.items():
        image = Path(path).resolve(strict=True)
        memory = cells[key]
        validate_image(MemorySpec('image', memory['width'], memory['depth'],
            implementation, 1 if implementation == 'register' else 2,
            False, image.name), image.parent)
        data = image.read_bytes()
        images[key] = image
        image_hashes[key] = hashlib.sha256(data).hexdigest()
        if narrow_immutable:
            width = memory['width']
            sign_bit = 1 << (width - 1)
            low, high, unsigned_high = 0, 0, 0
            for row in data.splitlines():
                word = int(row, 16)
                signed = word - (1 << width) if word & sign_bit else word
                low, high = min(low, signed), max(high, signed)
                unsigned_high = max(unsigned_high, word)
            bounds[key] = (low, high, unsigned_high)
    storage = {}
    if narrow_immutable:
        for representative, keys in groups.items():
            if mutable[representative]:
                continue
            physical = [key for key in keys if cells[key]['physical']]
            low = min(bounds[key][0] for key in physical)
            high = max(bounds[key][1] for key in physical)
            unsigned_high = max(bounds[key][2] for key in physical)
            signed = low < 0
            needed = (max((~low).bit_length(), high.bit_length()) + 1
                      if signed else max(1, unsigned_high.bit_length()))
            if needed < cells[physical[0]]['width']:
                storage[representative] = dict(storage_width=needed, sign_extend=signed)
    bindings = []
    for key, memory in sorted(cells.items()):
        image = images.get(key)
        selected = 'ddr3' if offchip_immutable and not mutable[root(key)] else implementation
        compact = dict(storage.get(root(key), {}))
        bits = memory['depth'] * compact.get('storage_width', memory['width'])
        if selected == 'bram' and lut_memory_max_bits and bits <= lut_memory_max_bits:
            selected = 'lutram'
        if selected == 'lutram' and not mutable[root(key)] and not compact:
            # Select the immutable logic-ROM primitive even when no bits can be
            # removed. Equal storage/port widths make this extension a no-op;
            # the native binder still verifies every image and reference.
            compact = dict(storage_width=memory['width'], sign_extend=False)
        latency = 0 if selected == 'ddr3' else (1 if selected == 'register' else 2)
        bindings.append(dict(component=key[0], cell=key[1], width=memory['width'],
            depth=memory['depth'], address_width=memory['address_width'],
            implementation=selected, read_latency=latency,
            writable=mutable[root(key)], init_file=image.name if image else None,
            init_sha256=image_hashes.get(key), **compact))
    return {'schema_version': 1, 'bindings': bindings}


def initializers_from_origins(inventory: dict, images: dict[str, Path]) -> dict:
    """Follow typed reference edges from frozen graph origins to physical cells.

    Compare decoded image words with the captured tensor's byte identity. An
    image filename or cell spelling alone cannot establish a buffer binding.
    """
    plan_memories(inventory, {})  # Validate geometry and reference completeness.
    cells = {(cell['component'], cell['cell']): cell for cell in inventory['memories']}
    incoming = {key: set() for key in cells}
    for edge in inventory['references']:
        source = (edge['caller'], edge['source'])
        target = (edge['callee'], edge['destination'])
        incoming[target].add(source)

    def physical_sources(start):
        # Reference arguments are rebound at each invoke. A shared callee does
        # not make its distinct physical callers the same tensor or storage.
        pending, visited, physical = [start], set(), set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            if cells[key]['physical']:
                physical.add(key)
            else:
                pending.extend(incoming[key] - visited)
        if not physical:
            raise ValueError('graph origin has no physical memory source')
        return physical

    identities, targets_by_cell, consumed, runtime = {}, {}, set(), set()
    for key, cell in cells.items():
        origin = cell.get('origin')
        if not origin:
            continue
        physical = physical_sources(key)
        if origin['kind'] == 'USER_INPUT':
            runtime.update(physical)
            continue
        if origin['kind'] not in ('BUFFER', 'PARAMETER', 'CONSTANT_TENSOR'):
            raise ValueError('unsupported graph memory origin')
        identity = origin.get('sha256')
        if identity is None:
            raise ValueError('ambiguous frozen graph tensor identity')
        target = origin['target']
        if target not in images:
            raise ValueError('missing frozen graph initializer: ' + target)
        if cell['width'] % 8:
            raise ValueError('tensor identity requires byte-aligned memory words')
        image = Path(images[target])
        validate_image(MemorySpec('image', cell['width'], cell['depth'], 'bram',
                                 2, False, image.name), image.parent)
        data = b''.join(int(word, 16).to_bytes(cell['width'] // 8, 'little')
                        for word in image.read_text().splitlines())
        if hashlib.sha256(data).hexdigest() != identity:
            raise ValueError('initializer differs from graph tensor identity: ' + target)
        consumed.add(target)
        for source in physical:
            if source in identities and identities[source] != identity:
                raise ValueError('ambiguous frozen graph tensor identity')
            identities[source] = identity
            targets_by_cell.setdefault(source, set()).add(target)
    if runtime.intersection(identities):
        raise ValueError('mixed frozen/runtime graph origins')
    if set(images) != consumed:
        raise ValueError('initializer has no frozen graph origin')
    return {key: images[min(targets)] for key, targets in targets_by_cell.items()}
