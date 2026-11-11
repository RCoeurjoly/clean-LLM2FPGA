"""Split a generated, typed transfer DAG into bounded Lean functions.

Only layout changes: each cut exports the exact node values and execution
domains needed after the cut. Ports, state, operation order and initialValid
are unchanged. This is not a hardware optimization or a partition in time.
"""
from __future__ import annotations

import re


def product_tree(values, leaf, pair, empty):
    if not values:
        return empty
    if len(values) == 1:
        return leaf(values[0])
    middle = len(values) // 2
    return pair(product_tree(values[:middle], leaf, pair, empty),
                product_tree(values[middle:], leaf, pair, empty))


def product_paths(names):
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: ''}
    middle = len(names) // 2
    return {name: side + ('.' + path if path else '')
            for side, group in [('fst', names[:middle]), ('snd', names[middle:])]
            for name, path in product_paths(group).items()}


def chunk_evaluation(ir, source, chunk_size):
    if chunk_size < 1:
        raise ValueError('Chunk size must be positive')
    count = len(ir['nodes'])
    if count <= chunk_size:
        return source
    lines = source.splitlines()
    marker = 'def evaluate (inputs : Inputs) (state : State) : Evaluation :='
    start = lines.index(marker)
    end = start + 1
    bindings, types, domains = {}, {}, set()
    binding = re.compile(r'  let ([nd])(\d+) : (.+?) := (.*)$')
    references = re.compile(r'\b([nd])(\d+)\b')
    seen = []
    while end < len(lines) and lines[end].startswith('  let '):
        match = binding.fullmatch(lines[end])
        if not match:
            raise ValueError('Unexpected transfer binding while splitting Lean')
        kind, serial, typ, value = match.groups()
        serial = int(serial)
        if kind == 'n':
            seen.append(serial)
            types[serial] = typ
        else:
            domains.add(serial)
        bindings.setdefault(serial // chunk_size, []).append((kind + str(serial), typ, value))
        end += 1
    if seen != list(range(count)):
        raise ValueError('Lean binding order differs from typed FunctionalIR')
    exports = {index: set() for index in bindings}
    incoming = {index: set() for index in bindings}
    for index, group in bindings.items():
        for _, _, value in group:
            for match in references.finditer(value):
                serial = int(match[2])
                owner = serial // chunk_size
                if owner > index:
                    raise ValueError('Transfer DAG references a future chunk')
                if owner < index:
                    exports[owner].add(serial)
                    incoming[index].add(owner)
    result_end = next(i for i in range(end, len(lines)) if lines[i].startswith('def step '))
    for line in lines[end:result_end]:
        for match in references.finditer(line):
            serial = int(match[2])
            exports[serial // chunk_size].add(serial)

    chunk_fields = {}
    for index in bindings:
        fields = []
        for serial in sorted(exports[index]):
            fields.append(('n' + str(serial), types[serial]))
            if serial in domains:
                fields.append(('d' + str(serial), 'Bool'))
        chunk_fields[index] = fields
    paths = {index: product_paths([name for name, _ in fields]) for index, fields in chunk_fields.items()}

    def rewrite(value, current=None):
        def replace(match):
            owner = int(match[2]) // chunk_size
            if owner == current:
                return match[0]
            path = paths[owner][match[0]]
            return 'c' + str(owner) + ('.' + path if path else '')
        return references.sub(replace, value)

    chunks = []
    for index, group in bindings.items():
        fields = chunk_fields[index]
        typ = product_tree(fields, lambda f: f[1], lambda a, b: '(' + a + ' × ' + b + ')', 'Unit')
        chunks += ['abbrev C2LChunk' + str(index) + ' := ' + typ]
        parameters = ''.join(f' (c{i} : C2LChunk{i})' for i in sorted(incoming[index]))
        chunks += ['', f'@[noinline] def c2lChunk{index} (inputs : Inputs) (state : State){parameters} : C2LChunk{index} :=']
        chunks += ['  let ' + name + ' : ' + typ + ' := ' + rewrite(value, index) for name, typ, value in group]
        record = product_tree(fields, lambda f: f[0], lambda a, b: '(' + a + ', ' + b + ')', '()')
        chunks += ['  ' + record, '']
    evaluation = [marker]
    for index in bindings:
        arguments = ''.join(' c' + str(i) for i in sorted(incoming[index]))
        evaluation.append(f'  let c{index} := c2lChunk{index} inputs state{arguments}')
    evaluation += [rewrite(line) for line in lines[end:result_end]]
    return '\n'.join(lines[:start] + chunks + evaluation + lines[result_end:]) + '\n'
