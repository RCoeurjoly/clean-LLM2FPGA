"""Prove unused ABI fields against the unchanged compiled Lean circuit data.

This accepts no live nondeterminism: even a disconnected input-reading node is
rejected. With no reading node, every choice of the extra field has exactly the
same evaluation, state transition, domains, events and diagnostic payloads.
"""
import hashlib
import mmap
import os
from pathlib import Path
import re
import stat


_NAT = rb'[\x80-\xff]*[\x00-\x7f]'
_SKIP = {n: re.compile(_NAT * n) for n in range(1, 33)}


class Decoder:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def nat(self):
        value = shift = 0
        while self.pos < len(self.data):
            byte = self.data[self.pos]
            self.pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError('Truncated compiled circuit integer')

    def skip(self, count):
        while count:
            size = min(count, 32)
            match = _SKIP[size].match(self.data, self.pos)
            if match is None:
                raise ValueError('Truncated compiled circuit integers')
            self.pos = match.end()
            count -= size

    def string(self, retain=True):
        size = self.nat()
        start = self.pos
        self.pos += size
        if self.pos > len(self.data):
            raise ValueError('Truncated compiled circuit string')
        return self.data[start:self.pos].decode('utf-8') if retain else None


def input_reads(data, ir, node_count):
    """Read the public v1 binary schema completely, without loading its DAG."""
    r = Decoder(data)
    if r.string() != 'chisel2lean-functional-1':
        raise ValueError('Unknown compiled circuit format')
    sorts = []
    for _ in range(r.nat()):
        kind = r.nat()
        if kind == 0: sorts.append(dict(kind='bits', width=r.nat()))
        elif kind == 1: sorts.append(dict(kind='bool'))
        elif kind == 2: sorts.append(dict(kind='memory', address_width=r.nat(), width=r.nat()))
        else: raise ValueError('Unknown compiled circuit sort')
    for kind in ('inputs', 'states'):
        if r.nat() != len(ir[kind]):
            raise ValueError('Compiled circuit field count differs from interface')
        for field in ir[kind]:
            name, sort_index = r.string(), r.nat()
            if name != field['name'] or sort_index >= len(sorts) or sorts[sort_index] != field['sort']:
                raise ValueError('Compiled circuit field differs from interface')
            known = r.nat()
            if kind == 'inputs' and known:
                raise ValueError('Unexpected compiled input constraints')
            r.skip(known * 2)
            next_node = r.nat()
            if kind == 'inputs' and next_node:
                raise ValueError('Unexpected compiled input transition')
    if r.nat() != len(ir['outputs']):
        raise ValueError('Compiled circuit output count differs from interface')
    for field in ir['outputs']:
        if r.string() != field['name']:
            raise ValueError('Compiled circuit output differs from interface')
        r.skip(3)  # node, optional guard, observation flag
    if r.nat() != len(ir.get('effects', [])):
        raise ValueError('Compiled circuit effect count differs from interface')
    for _ in ir.get('effects', []):
        r.skip(4)  # kind, enable, optional condition and arguments
        r.string(False); r.string(False); r.skip(1)
        r.string(False); r.string(False)
    clock = next((i + 1 for i, f in enumerate(ir['inputs']) if f['name'] == ir.get('clock')), 0)
    if r.nat() != clock or r.nat() != node_count:
        raise ValueError('Compiled circuit clock or node count differs')
    reads = set()
    for _ in range(node_count):
        operation, sort_index, arity = r.nat(), r.nat(), r.nat()
        if operation > 28 or sort_index >= len(sorts):
            raise ValueError('Unknown compiled circuit node')
        if operation == 1:
            if arity:
                raise ValueError('Malformed compiled input node')
            index = r.nat()
            if index >= len(ir['inputs']) or sorts[sort_index] != ir['inputs'][index]['sort']:
                raise ValueError('Compiled input node differs from interface')
            reads.add(index)
            r.skip(1)  # source line
        else:
            r.skip(arity + 2)  # operands, extra natural, source line
    if r.pos != len(data):
        raise ValueError('Trailing compiled circuit bytes')
    return reads


def prove_unused(model, manifest, ir, interface_sha256):
    arbitrary = manifest['arbitrary_inputs']
    if arbitrary != ir['arbitrary_inputs']:
        raise ValueError('Arbitrary-input metadata differs')
    if not arbitrary:
        return set(), None
    relative = manifest.get('interface') or manifest['ir']
    if interface_sha256 != manifest['artifact_sha256'][relative]:
        raise ValueError('Replay interface differs from public manifest SHA256')
    names = {a['name'] for a in arbitrary}
    fields = {f['name']: f for f in ir['inputs']}
    if len(names) != len(arbitrary) or len(fields) != len(ir['inputs']):
        raise ValueError('Duplicate arbitrary/input field')
    for a in arbitrary:
        if not re.fullmatch(r'c2l_undefined_[0-9]+(?:_Y)?', a['name']) or a['name'] not in fields or fields[a['name']]['sort'] != dict(kind='bits', width=a['width']):
            raise ValueError('Unexpected arbitrary-input metadata')
    root = Path(model).absolute()
    relative = manifest['table']['binary']
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Unsafe compiled circuit path')
    path = root / relative
    path.absolute().relative_to(root)
    for parent in [path, *path.parents]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Unsafe compiled circuit path')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError('Single-link regular compiled circuit required')
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before) or not before.st_size:
            raise ValueError('Compiled circuit changed before read')
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            digest = hashlib.sha256(data).hexdigest()
            if digest != manifest['artifact_sha256'][relative]:
                raise ValueError('Compiled circuit differs from public manifest SHA256')
            reads = input_reads(data, ir, manifest['nodes'])
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Compiled circuit changed during proof')
    if identity(path.lstat()) != identity(before):
        raise ValueError('Compiled circuit path changed during proof')
    live = {ir['inputs'][i]['name'] for i in reads} & names
    if live:
        raise ValueError('Unexpected arbitrary input is read by compiled Lean circuit: ' + sorted(live)[0])
    return names, dict(status='UNREFERENCED_COMPILED_INPUTS_PROVED', inputs=len(names),
                       read_input_fields=len(reads), nodes=manifest['nodes'], binary_sha256=digest,
                       interface_sha256=interface_sha256,
                       rule='No compiled input-reading node refers to any extra field; all assignments give identical evaluation',
                       abi_representative=0)
