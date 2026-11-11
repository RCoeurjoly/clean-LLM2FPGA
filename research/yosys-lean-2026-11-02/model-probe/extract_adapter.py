#!/usr/bin/env python3
"""Reconstruct the pinned TorchLean default Python capture adapter for a spike.

No Lean compiler is installed locally. This deliberately small evaluator handles
only the literal-string emitter in the audited source, rejecting other syntax.
It does not check Lean proofs or change operator lowering. Reproduce with the
upstream generated adapter before using the result beyond frontend diagnosis.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Emitter:
    def __init__(self, root):
        self.source = root / 'NN/Runtime/PyTorch/Export/TorchExport.lean'
        self.wire = root / 'NN/Runtime/PyTorch/Wire.lean'
        self.text = self.source.read_text()
        self.wire_text = self.wire.read_text()
        tag_block = self.wire_text.split('def tag : OpTag → String', 1)[1].split('/--', 1)[0]
        self.tags = dict(re.findall(r'\| \.(\w+) => "([^"]+)"', tag_block))

    def wire_value(self, name, tag=None):
        if tag:
            quoted = json.dumps(self.tags[tag])
            return {'quoted': quoted, 'field': '"kind": ' + quoted,
                    'object': '{"kind": ' + quoted + '}'}[name]
        match = re.search(r'def ' + name + r' : String := ("(?:\\.|[^"\\])*")',
                          self.wire_text)
        if not match:
            raise ValueError('unknown wire value ' + name)
        return json.loads(match[1])

    def expression(self, text):
        token = re.compile(r'\s*(s!"(?:\\.|[^"\\])*"|"(?:\\.|[^"\\])*"|'
                           r'Wire\.\w+|options\.\w+|\.\w+|\d+|\+\+|[()]|\w+)')
        pos, tokens = 0, []
        while pos < len(text):
            if not text[pos:].strip():
                break
            match = token.match(text, pos)
            if not match:
                raise ValueError('unrecognized expression ' + repr(text[pos:]))
            tokens.append(match[1])
            pos = match.end()
        index = 0

        def term():
            nonlocal index
            current = tokens[index]
            index += 1
            if current == '(':
                result = concat()
                assert tokens[index] == ')'
                index += 1
                return result
            if current.startswith(('"', 's!"')):
                value = json.loads(current[2:] if current.startswith('s!') else current)
                return value.replace('{options.functionName}', 'export_torchlean_graph_json')
            if current == 'indent':
                amount = int(tokens[index])
                index += 1
                return ' ' * amount + term()
            if current == 'boolLiteral':
                assert tokens[index] in ('options.preferTorchExport', 'options.includeDebugTargets')
                index += 1
                return 'True'
            if current.startswith('Wire.'):
                name = current[5:]
                tag = None
                if name in ('quoted', 'field', 'object'):
                    tag = tokens[index][1:]
                    index += 1
                return self.wire_value(name, tag)
            raise ValueError('unhandled token ' + current)

        def concat():
            nonlocal index
            result = term()
            while index < len(tokens) and tokens[index] == '++':
                index += 1
                result += term()
            return result

        value = concat()
        assert index == len(tokens), tokens[index:]
        return value

    def array(self, name):
        match = re.search(r'^def ' + name + r'[^\n]*: Array String :=\s*#\[',
                          self.text, re.M)
        if not match:
            raise ValueError('missing literal array ' + name)
        start, pos, depth, in_string, escaped = match.end(), match.end(), 0, False, False
        expressions = []
        while pos < len(self.text):
            char = self.text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif depth == 0 and char in ',]':
                expressions.append(self.text[start:pos].strip())
                start = pos + 1
                if char == ']':
                    return [self.expression(value) for value in expressions]
            pos += 1
        raise ValueError('unterminated array ' + name)

    def pointwise(self):
        raw = self.text.split('pythonBlock r#"', 1)[1].split('"#', 1)[0]
        pairs = self.text.split('def pointwiseKinds : String :=', 1)[1].split('/--', 1)[0]
        names = re.findall(r'\("([^"]+)",', pairs)
        values = {'KINDS': '{' + ', '.join(repr(n) + ': ' + json.dumps(n) for n in names) + '}',
                  'GELU_FIELD': self.wire_value('geluApproximateField'),
                  'DIV_FIELD': self.wire_value('divRoundingField')}
        for name, tag in [('CAST', 'custom'), ('BROADCAST', 'broadcastTo'), ('INV', 'inv'),
                          ('MUL', 'mulElem'), ('SQRT', 'sqrt'), ('ADD', 'add'), ('SUB', 'sub')]:
            values[name] = self.wire_value('quoted', tag)
        for key, value in values.items():
            raw = raw.replace('@' + key + '@', value)
        assert not re.search(r'@\w+@', raw)
        return raw.split('\n')

    def emit(self):
        arrays = []
        for name in ['imports', 'shapes', 'tupleKind', 'model', 'routing', 'payload', 'axes',
                     'pointwise', 'header', 'pointwiseRule', 'modules', 'elementwise',
                     'tensorOps', 'layers', 'spatial', 'coverage', 'capture', 'entrypoint', 'cli']:
            arrays += self.pointwise() if name == 'pointwise' else self.array(name)
        script = '\n'.join(arrays)
        ast.parse(script)
        return script


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_root', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    emitter = Emitter(args.source_root)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(emitter.emit())
    print(json.dumps({'adapter': str(args.destination), 'sha256': digest(args.destination),
                      'upstream_exporter_sha256': digest(emitter.source),
                      'upstream_wire_sha256': digest(emitter.wire),
                      'lean_compiler_used': False}, indent=2))
