"""Compact, read-only storage for large typed FunctionalIR node sequences."""
from __future__ import annotations

from array import array
from collections.abc import Sequence


class PackedNodes(Sequence):
    def __init__(self):
        self.operations, self.operation_ids = [], {}
        self.sorts, self.sort_ids = [], {}
        self.op = array('B')
        self.sort = array('I')
        self.starts = array('I', [0])
        self.arguments = array('I')
        self.extra = array('I')
        self.lines = array('I')
        self.flags = array('B')
        self.constants = []

    def __len__(self):
        return len(self.op)

    def append(self, node):
        if node['id'] != len(self):
            raise ValueError('Packed FunctionalIR requires consecutive node IDs')
        extra_keys = {'constant': {'value'}, 'input': {'field'}, 'state': {'field'}, 'extract': {'offset'}}
        unknown = set(node) - {'id', 'op', 'sort', 'args', 'line'} - extra_keys.get(node['op'], set())
        if unknown:
            raise ValueError('Unsupported packed node data: ' + repr(unknown))
        operation = node['op']
        if operation not in self.operation_ids:
            if len(self.operations) == 256:
                raise ValueError('Too many FunctionalIR operation kinds')
            self.operation_ids[operation] = len(self.operations)
            self.operations.append(operation)
        signature = tuple(sorted(node['sort'].items()))
        if signature not in self.sort_ids:
            self.sort_ids[signature] = len(self.sorts)
            self.sorts.append(node['sort'])
        args = node['args']
        if any(a < 0 or a >= len(self) for a in args):
            raise ValueError('Packed FunctionalIR must be an ordered DAG')
        extra = 0
        if operation == 'constant':
            extra = len(self.constants)
            self.constants.append(node['value'])
        elif operation in {'input', 'state'}:
            prefix = 'i' if operation == 'input' else 's'
            if not node['field'].startswith(prefix) or not node['field'][1:].isdigit():
                raise ValueError('Malformed packed FunctionalIR field identifier')
            extra = int(node['field'][1:])
        elif operation == 'extract':
            extra = node['offset']
        line = node.get('line', 0)
        values = [extra, line, len(self.arguments) + len(args), self.sort_ids[signature]]
        if any(value < 0 or value >= 1 << 32 for value in values):
            raise ValueError('FunctionalIR exceeds the 32-bit packed index contract')
        self.op.append(self.operation_ids[operation])
        self.sort.append(self.sort_ids[signature])
        self.arguments.extend(args)
        self.starts.append(len(self.arguments))
        self.extra.append(extra)
        self.lines.append(line)
        self.flags.append(int('line' in node))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self))) ]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        operation = self.operations[self.op[index]]
        node = {'id': index, 'op': operation, 'sort': self.sorts[self.sort[index]],
                'args': list(self.arguments[self.starts[index]:self.starts[index + 1]])}
        if operation == 'constant':
            node['value'] = self.constants[self.extra[index]]
        elif operation in {'input', 'state'}:
            node['field'] = ('i' if operation == 'input' else 's') + str(self.extra[index])
        elif operation == 'extract':
            node['offset'] = self.extra[index]
        if self.flags[index]:
            node['line'] = self.lines[index]
        return node

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def storage_bytes(self):
        return sum(len(values) * values.itemsize for values in
                   (self.op, self.sort, self.starts, self.arguments, self.extra, self.lines, self.flags))
