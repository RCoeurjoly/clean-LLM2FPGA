"""Read the pinned FunctionalIR serialization without retaining its let tree.

The first pass retains record declarations and the returned pair. The second
pass yields one binding at a time. Initial assertions are syntax-checked and
discarded, just as the ordinary importer discards their semantics.
"""
from __future__ import annotations

from collections import deque
import re

TOKEN = re.compile(r'\s+|;[^\r\n]*|[()]|[^\s();]+')


class Reader:
    def __init__(self, stream):
        self.stream = stream
        self.tokens = self.lex()
        self.pending = deque()
        self.last = None

    def lex(self):
        for line, text in enumerate(self.stream, 1):
            for match in TOKEN.finditer(text):
                value = match[0]
                if value.isspace():
                    continue
                if value[0] in {'|', '"'}:
                    raise ValueError(f'SMT line {line}: quoted atoms are outside the locked serializer contract')
                yield value, line

    def peek(self):
        while not self.pending:
            token = next(self.tokens, None)
            if token is None:
                return None
            value, _ = token
            if value.startswith(';'):
                if self.last is not None:
                    self.last.comment = value[1:].strip()
            else:
                self.pending.append(token)
        return self.pending[0]

    def take(self, expected=None):
        token = self.peek()
        if token is None:
            raise ValueError('SMT: unexpected end of serialization')
        self.pending.popleft()
        if expected is not None and token[0] != expected:
            raise ValueError(f'SMT line {token[1]}: expected {expected!r}, got {token[0]!r}')
        return token

    def form(self):
        from functional_ir import SExpr
        value, line = self.take()
        if value == ')':
            raise ValueError(f'SMT line {line}: unmatched closing parenthesis')
        if value == '(':
            return self.list_rest(SExpr([], line))
        node = SExpr(value, line)
        self.last = node
        self.peek()  # Preserve comments attached to returned field atoms.
        return node

    def list_rest(self, root):
        from functional_ir import SExpr
        stack = [root]
        while stack:
            value, line = self.take()
            if value == '(':
                child = SExpr([], line)
                stack[-1].value.append(child)
                stack.append(child)
            elif value == ')':
                self.last = stack.pop()
            else:
                self.last = SExpr(value, line)
                stack[-1].value.append(self.last)
        self.peek()
        return root

    def skip_rest(self):
        depth = 1
        self.last = None
        while depth:
            value, _ = self.take()
            depth += (value == '(') - (value == ')')


def scan(path, keep_roots):
    from functional_ir import SExpr, atom, items
    snapshot = path.stat()
    with path.open(encoding='utf-8') as stream:
        reader = Reader(stream)
        while reader.peek() is not None:
            _, line = reader.take('(')
            head = reader.form()
            kind = atom(head)
            if kind == 'define-fun':
                name, parameters, result = reader.form(), reader.form(), reader.form()
                depth = 0
                while True:
                    _, body_line = reader.take('(')
                    operation = reader.form()
                    if atom(operation) == 'let':
                        group = items(reader.form())
                        if len(group) != 1 or len(items(group[0])) != 2:
                            raise ValueError('Expected the upstream serializer\'s single-binding let')
                        variable, expression = items(group[0])
                        if not keep_roots:
                            yield 'binding', (atom(variable), expression)
                        depth += 1
                    elif atom(operation) == 'pair':
                        if keep_roots:
                            body = reader.list_rest(SExpr([operation], body_line))
                        else:
                            reader.skip_rest()
                            body = None
                        break
                    else:
                        raise ValueError(f'SMT line {body_line}: expected ordered let or return pair')
                for _ in range(depth + 1):
                    reader.take(')')
                if keep_roots:
                    root = SExpr([head, name, parameters, result, body], line)
                    reader.last = root
                    yield 'root', root
            elif kind in {'declare-datatype', 'declare-datatypes'}:
                if keep_roots:
                    yield 'root', reader.list_rest(SExpr([head], line))
                else:
                    reader.skip_rest()
            elif kind in {'declare-const', 'assert'}:
                reader.skip_rest()
            else:
                raise ValueError(f'SMT line {line}: unsupported FunctionalIR root {kind}')
    after = path.stat()
    if (snapshot.st_size, snapshot.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('FunctionalIR serialization changed while reading')


def transfer(path):
    return [value for kind, value in scan(path, True) if kind == 'root']


def bindings(path):
    for kind, value in scan(path, False):
        if kind == 'binding':
            yield value
