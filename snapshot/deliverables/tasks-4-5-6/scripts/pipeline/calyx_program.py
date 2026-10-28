"""Structured Calyx frontend for the generated single-component subset.

Regex is used only for lexical recognition. Transformations operate on cells,
port references and assignments; serialized program text is never substituted.
Unsupported syntax fails instead of being silently copied around a rewrite.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import re

_TOKEN = re.compile(r'''\s+|//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\d+'[dbx][0-9a-fA-F_]+|[A-Za-z_][A-Za-z0-9_]*|\d+|->|==|!=|>=|<=|[{}()[\].,:;=@?&|!+*/<>-]''', re.S)


def lex(source: str) -> list[str]:
    tokens = []
    cursor = 0
    while cursor < len(source):
        match = _TOKEN.match(source, cursor)
        if match is None:
            raise ValueError(f'unsupported Calyx token at {source[cursor:cursor + 30]!r}')
        token = match.group()
        cursor = match.end()
        if not token.isspace() and not token.startswith(('//', '/*')):
            tokens.append(token)
    return tokens


@dataclass(frozen=True)
class Reference:
    cell: str
    port: str
    hole: bool = False

    def serialize(self) -> str:
        if not self.cell:
            return self.port
        return f'{self.cell}[{self.port}]' if self.hole else f'{self.cell}.{self.port}'


@dataclass
class Expression:
    parts: list[str | Reference]

    @classmethod
    def parse(cls, tokens: list[str]) -> Expression:
        parts = []
        index = 0
        while index < len(tokens):
            if index + 2 < len(tokens) and tokens[index + 1] == '.':
                parts.append(Reference(tokens[index], tokens[index + 2]))
                index += 3
            elif index + 3 < len(tokens) and tokens[index + 1] == '[' and tokens[index + 3] == ']':
                parts.append(Reference(tokens[index], tokens[index + 2], True))
                index += 4
            elif re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', tokens[index]):
                parts.append(Reference('', tokens[index]))
                index += 1
            else:
                parts.append(tokens[index])
                index += 1
        return cls(parts)

    @classmethod
    def text(cls, source: str) -> Expression:
        return cls.parse(lex(source))

    def serialize(self) -> str:
        rendered = []
        previous = None
        for part in self.parts:
            token = part.serialize() if isinstance(part, Reference) else part
            separator = '' if not rendered or token in (';', ',', ')', ']') or previous in ('(', '[', '@') else ' '
            rendered.append(separator + token)
            previous = token
        return ''.join(rendered)

    def rename(self, names: dict[str, str]) -> None:
        self.parts = [Reference(names.get(part.cell, part.cell), part.port, part.hole)
                      if isinstance(part, Reference) else names.get(part, part) for part in self.parts]


@dataclass
class Control:
    """Control tree for the dynamic subset emitted by this frontend.

    Root is an implicit list; seq/par own child statements, while/if own a
    condition and body, and enable names one group. Unsupported forms fail.
    """
    kind: str
    children: list[Control] = field(default_factory=list)
    name: str | None = None
    condition: Expression | None = None
    comb_group: str | None = None
    otherwise: Control | None = None

    @classmethod
    def parse(cls, source: str) -> Control:
        return cls.from_tokens(lex(source))

    @classmethod
    def from_tokens(cls, tokens: list[str]) -> Control:
        cursor = 0

        def body() -> Control:
            nonlocal cursor
            if cursor >= len(tokens) or tokens[cursor] != '{':
                raise ValueError('control statement requires a body')
            inner, after = _block(tokens, cursor)
            cursor = after
            return cls.from_tokens(inner)

        children = []
        while cursor < len(tokens):
            kind = tokens[cursor]
            cursor += 1
            if kind in ('seq', 'par'):
                children.append(cls(kind, body().children))
            elif kind in ('while', 'if'):
                start = cursor
                while cursor < len(tokens) and tokens[cursor] not in ('with', '{'):
                    cursor += 1
                condition = Expression.parse(tokens[start:cursor])
                if len(condition.parts) != 1 or not isinstance(condition.parts[0], Reference):
                    raise ValueError('control condition must be a port reference')
                comb_group = None
                if cursor < len(tokens) and tokens[cursor] == 'with':
                    cursor += 1
                    if cursor >= len(tokens) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', tokens[cursor]):
                        raise ValueError('invalid control condition group')
                    comb_group = tokens[cursor]
                    cursor += 1
                node = cls(kind, body().children, condition=condition, comb_group=comb_group)
                if kind == 'if' and cursor < len(tokens) and tokens[cursor] == 'else':
                    cursor += 1
                    node.otherwise = body()
                children.append(node)
            elif kind == ';':
                children.append(cls('empty'))
            elif re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', kind) and cursor < len(tokens) and tokens[cursor] == ';':
                cursor += 1
                children.append(cls('enable', name=kind))
            else:
                raise ValueError(f'unsupported control statement {kind}')
        return cls('root', children)

    def serialize(self) -> str:
        contents = ' '.join(child.serialize() for child in self.children)
        if self.kind == 'root':
            return contents
        if self.kind == 'enable':
            return self.name + ';'
        if self.kind == 'empty':
            return ';'
        if self.kind in ('seq', 'par'):
            return self.kind + ' { ' + contents + ' }'
        if self.kind in ('while', 'if'):
            group = ' with ' + self.comb_group if self.comb_group else ''
            result = self.kind + ' ' + self.condition.serialize() + group + ' { ' + contents + ' }'
            if self.otherwise is not None:
                result += ' else { ' + self.otherwise.serialize() + ' }'
            return result
        raise ValueError(f'unsupported control node {self.kind}')

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()
        if self.otherwise is not None:
            yield from self.otherwise.walk()

    def rename(self, names: dict[str, str]) -> None:
        for node in self.walk():
            if node.name is not None:
                node.name = names.get(node.name, node.name)
            if node.comb_group is not None:
                node.comb_group = names.get(node.comb_group, node.comb_group)
            if node.condition is not None:
                node.condition.rename(names)

    def insert_before(self, group: str, predecessor: str) -> None:
        self._insert(group, predecessor, after=False)

    def insert_after(self, group: str, successor: str) -> None:
        self._insert(group, successor, after=True)

    def _insert(self, group: str, added: str, *, after: bool) -> None:
        sites = [(parent, index) for parent in self.walk()
                 for index, child in enumerate(parent.children)
                 if child.kind == 'enable' and child.name == group]
        if len(sites) != 1:
            raise ValueError(f'expected exactly one enable of {group}, found {len(sites)}')
        parent, index = sites[0]
        enable = Control('enable', name=added)
        if parent.kind in ('root', 'seq'):
            parent.children.insert(index + int(after), enable)
        else:
            original = parent.children[index]
            parent.children[index] = Control('seq', [original, enable] if after else [enable, original])


@dataclass
class Assignment:
    target: Reference
    value: Expression

    def serialize(self) -> str:
        return f'{self.target.serialize()} = {self.value.serialize()};'


@dataclass
class Cell:
    name: str
    primitive: str
    parameters: list[int]
    attributes: list[str] = field(default_factory=list)

    def serialize(self) -> str:
        prefix = (' '.join(self.attributes) + ' ') if self.attributes else ''
        return prefix + f'{self.name} = {self.primitive}(' + ', '.join(map(str, self.parameters)) + ');'


@dataclass
class Group:
    header: list[str]
    assignments: list[Assignment]

    @property
    def name(self) -> str:
        return self.header[self.header.index('group') + 1]

    def serialize(self) -> str:
        return ' '.join(self.header) + ' {\n' + '\n'.join('      ' + a.serialize() for a in self.assignments) + '\n    }'


def _block(tokens: list[str], opening: int) -> tuple[list[str], int]:
    if tokens[opening] != '{':
        raise ValueError('expected block')
    depth = 1
    for end in range(opening + 1, len(tokens)):
        depth += (tokens[end] == '{') - (tokens[end] == '}')
        if depth == 0:
            return tokens[opening + 1:end], end + 1
    raise ValueError('unclosed Calyx block')


def _assignment(tokens: list[str]) -> Assignment:
    separator = tokens.index('=')
    lhs = Expression.parse(tokens[:separator]).parts
    if len(lhs) == 1 and isinstance(lhs[0], str) and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', lhs[0]):
        lhs = [Reference('', lhs[0])]
    if len(lhs) != 1 or not isinstance(lhs[0], Reference):
        raise ValueError('assignment target must be a port or group hole')
    return Assignment(lhs[0], Expression.parse(tokens[separator + 1:]))


@dataclass
class Program:
    prefix: list[str]
    cells: dict[str, Cell]
    wires: list[Assignment | Group]
    control: Control
    suffix: list[str]

    @classmethod
    def parse(cls, source: str) -> Program:
        tokens = lex(source)
        component = tokens.index('component')
        body_open = tokens.index('{', component)
        body, end = _block(tokens, body_open)
        if end != len(tokens):
            raise ValueError('structured frontend supports a single component only')
        sections = {}
        cursor = 0
        while cursor < len(body):
            name = body[cursor]
            if name not in ('cells', 'wires', 'control') or name in sections:
                raise ValueError(f'unsupported or duplicate component section {name}')
            sections[name], cursor = _block(body, cursor + 1)
        if set(sections) != {'cells', 'wires', 'control'}:
            raise ValueError('component requires cells, wires, control')
        cells = {}
        raw = sections['cells']
        cursor = 0
        while cursor < len(raw):
            stop = raw.index(';', cursor)
            statement = raw[cursor:stop]
            equal = statement.index('=')
            name = statement[equal - 1]
            if name in cells:
                raise ValueError(f'duplicate cell {name}')
            if statement[equal + 2] != '(' or statement[-1] != ')':
                raise ValueError('unsupported cell declaration')
            parameters = [int(token) for token in statement[equal + 3:-1] if token != ',']
            cells[name] = Cell(name, statement[equal + 1], parameters, statement[:equal - 1])
            cursor = stop + 1
        wires = []
        raw = sections['wires']
        cursor = 0
        while cursor < len(raw):
            if raw[cursor] in ('group', 'comb', 'static', '@'):
                opening = raw.index('{', cursor)
                statements, after = _block(raw, opening)
                assignments = []
                while statements:
                    stop = statements.index(';')
                    assignments.append(_assignment(statements[:stop]))
                    statements = statements[stop + 1:]
                wires.append(Group(raw[cursor:opening], assignments))
                cursor = after
            else:
                stop = raw.index(';', cursor)
                wires.append(_assignment(raw[cursor:stop]))
                cursor = stop + 1
        return cls(tokens[:body_open + 1], cells, wires, Control.from_tokens(sections['control']), tokens[end - 1:])

    def serialize(self) -> str:
        return (' '.join(self.prefix) + '\n  cells {\n' +
                '\n'.join('    ' + c.serialize() for c in self.cells.values()) +
                '\n  }\n  wires {\n' + '\n'.join('    ' + w.serialize() for w in self.wires) +
                '\n  }\n  control {\n    ' + self.control.serialize() + '\n  }\n' + ' '.join(self.suffix) + '\n')

    def assignments(self):
        for wire in self.wires:
            yield from wire.assignments if isinstance(wire, Group) else [wire]

    def rename(self, names: dict[str, str]) -> None:
        targets = [names.get(name, name) for name in self.cells]
        if len(set(targets)) != len(targets):
            raise ValueError('cell rename collision')
        renamed = {}
        for cell in self.cells.values():
            cell.name = names.get(cell.name, cell.name)
            renamed[cell.name] = cell
        self.cells = renamed
        for wire in self.wires:
            if isinstance(wire, Group):
                wire.header = [names.get(token, token) for token in wire.header]
        for assignment in self.assignments():
            ref = assignment.target
            assignment.target = Reference(names.get(ref.cell, ref.cell), ref.port, ref.hole)
            assignment.value.rename(names)
        self.control.rename(names)

    def resize_memory(self, name: str, width: int, depth: int) -> None:
        cell = self.cells[name]
        if cell.primitive != 'seq_mem_d1' or width < 1 or depth < 1:
            raise ValueError('invalid memory resize')
        selected = [a for a in self.assignments() if a.target == Reference(name, 'addr0')]
        for assignment in selected:
            parts = assignment.value.parts
            literal = re.fullmatch(r"(\d+)'d(\d+)", parts[0]) if len(parts) == 1 and isinstance(parts[0], str) else None
            if literal and int(literal[2]) >= depth:
                raise ValueError(f'{name}: address exceeds resized depth')
            if not literal and depth < cell.parameters[1]:
                raise ValueError(f'{name}: dynamic address needs a bound certificate before shrinking')
        address_width = max(1, (depth - 1).bit_length())
        cell.parameters = [width, depth, address_width]
        for assignment in selected:
            parts = assignment.value.parts
            if len(parts) == 1 and isinstance(parts[0], str) and "'d" in parts[0]:
                value = int(parts[0].split("'d")[1])
                assignment.value = Expression.text(f"{address_width}'d{value}")

    def alias_memories(self, aliases: dict[str, str]) -> None:
        replacements = {}
        for old in aliases:
            target = old
            seen = set()
            while target in aliases:
                if target in seen:
                    raise ValueError('cyclic memory alias')
                seen.add(target)
                target = aliases[target]
            if old not in self.cells or target not in self.cells:
                raise ValueError('alias memory is undeclared')
            source, destination = self.cells[old], self.cells[target]
            if source.primitive != 'seq_mem_d1' or destination.primitive != 'seq_mem_d1' or source.parameters[0] != destination.parameters[0]:
                raise ValueError('alias memory width/primitive mismatch')
            replacements[old] = target
        for old, target in replacements.items():
            source, destination = self.cells[old], self.cells[target]
            self.resize_memory(target, destination.parameters[0], max(destination.parameters[1], source.parameters[1]))
        for old in replacements:
            del self.cells[old]
        self.rename(replacements)

    def bind_reference(self, cell: str, port: str, expression: str) -> None:
        selected = [a for a in self.assignments() if a.target == Reference(cell, port)]
        if not selected:
            raise ValueError(f'missing assignment {cell}.{port}')
        for assignment in selected:
            assignment.value = Expression.text(expression)

    def narrow_memory(self, name: str, width: int, *, writable: bool) -> None:
        cell = self.cells[name]
        if cell.primitive != 'seq_mem_d1' or cell.parameters[0] != 64 or not 0 < width <= 64:
            raise ValueError('narrowing requires a 64-bit sequential memory')
        adapters = [name + '_wide'] + ([name + '_narrow'] if writable else [])
        if any(adapter in self.cells for adapter in adapters):
            raise ValueError('narrowing adapter collision')
        writes = [a for a in self.assignments() if a.target == Reference(name, 'write_data')]
        if writable and not writes:
            raise ValueError('runtime memory has no write assignment')
        if not writable and writes:
            raise ValueError('immutable memory has a write assignment')
        cell.parameters[0] = width
        self.cells[name + '_wide'] = Cell(name + '_wide', 'std_signext', [width, 64])
        if writable:
            self.cells[name + '_narrow'] = Cell(name + '_narrow', 'std_slice', [64, width])
        for assignment in self.assignments():
            assignment.value.parts = [Reference(name + '_wide', 'out') if part == Reference(name, 'read_data') else part for part in assignment.value.parts]
        for wire in list(self.wires):
            assignments = wire.assignments if isinstance(wire, Group) else [wire]
            for assignment in list(assignments):
                if assignment.target == Reference(name, 'write_data'):
                    assignment.target = Reference(name + '_narrow', 'in')
                    extra = Assignment(Reference(name, 'write_data'), Expression.text(name + '_narrow.out'))
                    if isinstance(wire, Group):
                        assignments.insert(assignments.index(assignment) + 1, extra)
                    else:
                        self.wires.append(extra)
        self.wires.insert(0, Assignment(Reference(name + '_wide', 'in'), Expression.text(name + '.read_data')))

    def bind_memories(self, bindings: dict[str, dict], declaration_path: str) -> None:
        import json
        memory_names = {name for name, cell in self.cells.items() if cell.primitive == 'seq_mem_d1'}
        if set(bindings) != memory_names:
            raise ValueError('explicit binding set differs from memory declarations')
        for name, binding in bindings.items():
            parameters = binding['parameters']
            if len(parameters) != 5 or parameters[:3] != self.cells[name].parameters:
                raise ValueError(f'{name}: binding geometry differs')
            if binding['primitive'] not in ('closure_register_memory', 'closure_bram_memory', 'closure_lutram_memory'):
                raise ValueError('unsupported explicit memory primitive')
        for name, binding in bindings.items():
            self.cells[name].primitive = binding['primitive']
            self.cells[name].parameters = list(binding['parameters'])
        self.prefix = ['import', json.dumps(declaration_path), ';', *self.prefix]
