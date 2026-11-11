"""Encode the checked FunctionalIR as data for the pure Lean interpreter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil

from common import ROOT
from functional_nodes import PackedNodes

OPERATIONS = ('constant', 'input', 'state', 'ite', 'concat', 'extract', 'zero_extend', 'sign_extend',
              'bvnot', 'bvneg', 'bvudiv', 'bvurem', 'bvshl', 'bvlshr', 'bvashr', 'bvadd', 'bvsub',
              'bvmul', 'bvand', 'bvor', 'bvxor', '=', 'distinct', 'bvugt', 'bvuge', 'bvsgt', 'bvsge',
              'select', 'store')
OPCODE = {name: index for index, name in enumerate(OPERATIONS)}


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = bytearray()

    def nat(self, value):
        if not isinstance(value, int) or value < 0:
            raise ValueError('Circuit integers must be nonnegative')
        while value >= 128:
            self.buffer.append((value & 127) | 128)
            value >>= 7
        self.buffer.append(value)
        if len(self.buffer) >= 1024 * 1024:
            self.flush()

    def string(self, value):
        data = value.encode('utf-8')
        self.nat(len(data))
        self.buffer.extend(data)

    def option(self, value):
        self.nat(0 if value is None else value + 1)

    def flush(self):
        self.stream.write(self.buffer)
        self.buffer.clear()


def emit_table(ir, stage, runtime_source=None):
    namespace = ir['namespace']
    if not re.fullmatch(r'[A-Z][A-Za-z0-9_]*(\.[A-Z][A-Za-z0-9_]*)*', namespace):
        raise ValueError('Namespace must be dot-separated uppercase-leading identifiers')
    relative = Path(*namespace.split('.'))
    model = stage / relative.with_suffix('.lean')
    model.parent.mkdir(parents=True, exist_ok=True)
    binary = model.with_suffix('.circuit.bin')
    hexadecimal = model.with_suffix('.circuit.hex')
    sorts, sort_ids = [], {}
    def signature(sort):
        return sort['kind'], sort.get('width'), sort.get('address_width')
    def insert(sort):
        key = signature(sort)
        if key not in sort_ids:
            sort_ids[key] = len(sorts)
            sorts.append(sort)
    for kind in ('inputs', 'states', 'outputs'):
        for field in ir[kind]:
            insert(field['sort'])
    if isinstance(ir['nodes'], PackedNodes):
        for sort in ir['nodes'].sorts:
            insert(sort)
    else:
        for node in ir['nodes']:
            insert(node['sort'])
    output_nodes = {field['name']: field['node'] for field in ir['outputs']}
    output_indices = {field['name']: index for index, field in enumerate(ir['outputs'])}
    with binary.open('wb') as target:
        writer = Writer(target)
        writer.string('chisel2lean-functional-1')
        writer.nat(len(sorts))
        for sort in sorts:
            writer.nat({'bits': 0, 'bool': 1, 'memory': 2}[sort['kind']])
            if sort['kind'] == 'memory':
                writer.nat(sort['address_width'])
            if sort['kind'] != 'bool':
                writer.nat(sort['width'])
        for kind in ('inputs', 'states'):
            writer.nat(len(ir[kind]))
            for field in ir[kind]:
                writer.string(field['name'])
                writer.nat(sort_ids[signature(field['sort'])])
                constraints = [] if kind == 'inputs' else (field['initial']['words'] if field['sort']['kind'] == 'memory' else [field['initial']])
                writer.nat(len(constraints))
                for known in constraints:
                    writer.nat(known['mask'])
                    writer.nat(known['value'])
                writer.option(field['node'] if kind == 'states' else None)
        writer.nat(len(ir['outputs']))
        for field in ir['outputs']:
            writer.string(field['name'])
            writer.nat(field['node'])
            writer.option(field['effect_enable_node'] if field.get('effect_role') in {'condition', 'arguments'} else None)
            writer.nat(int('effect' in field))
        writer.nat(len(ir.get('effects', [])))
        for index, effect in enumerate(ir.get('effects', [])):
            if effect['index'] != index:
                raise ValueError('Observation IDs differ from their model order')
            writer.nat({'assert': 0, 'assume': 1, 'cover': 2, 'print': 3}[effect['kind']])
            writer.nat(output_indices[effect['ports']['enable']])
            for role in ('condition', 'arguments'):
                writer.option(output_indices[effect['ports'][role]] if role in effect['ports'] else None)
            writer.string(effect['source'])
            writer.string(effect['format'])
            writer.option(effect.get('file_descriptor'))
            writer.string(effect.get('stream', ''))
            priority = effect.get('priority_bits', '')
            writer.string(priority if isinstance(priority, str) else str(priority))
        clock = next((i for i, field in enumerate(ir['inputs']) if field['name'] == ir.get('clock')), None)
        writer.option(clock)
        writer.nat(len(ir['nodes']))
        for node in ir['nodes']:
            writer.nat(OPCODE[node['op']])
            writer.nat(sort_ids[signature(node['sort'])])
            writer.nat(len(node['args']))
            for argument in node['args']:
                writer.nat(argument)
            extra = 0
            if node['op'] == 'constant':
                extra = int(node['value'])
            elif node['op'] in {'input', 'state'}:
                extra = int(node['field'][1:])
            elif node['op'] == 'extract':
                extra = node['offset']
            writer.nat(extra)
            writer.nat(node.get('line', 0))
        writer.flush()
    with binary.open('rb') as source, hexadecimal.open('w', encoding='ascii', newline='') as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            target.write(chunk.hex())
    runtime = stage / 'Chisel2Lean/Functional.lean'
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(runtime_source if runtime_source is not None else (ROOT / 'lean/Chisel2Lean/Functional.lean').read_bytes())
    model.write_text(f'''import Chisel2Lean.Functional
namespace {namespace}
open Chisel2Lean.Functional
abbrev Inputs := Array Value
abbrev State := Array Value
def circuit : Except String CheckedCircuit := decodeHex (include_str {json.dumps(hexadecimal.name)})
def evaluate (inputs : Inputs) (state : State) : Except String Evaluation := do
  Chisel2Lean.Functional.evaluate (← circuit).data inputs state
def step (inputs : Inputs) (state : State) : Except String (Array Value × State) := do
  let result ← evaluate inputs state
  return (result.outputs, result.nextState)
def initialValid (state : State) : Except String Bool := do
  Chisel2Lean.Functional.initialValid (← circuit).data.states state
#eval do
  let checked ← match circuit with
    | .ok value => pure value
    | .error message => throw (IO.userError message)
  IO.println ("C2L_TABLE_CHECK " ++ (Lean.Json.mkObj [
    ("nodes", Lean.toJson checked.data.nodes.size),
    ("inputs", Lean.toJson checked.data.inputs.size),
    ("states", Lean.toJson checked.data.states.size),
    ("outputs", Lean.toJson checked.data.outputs.size)]).compress)
end {namespace}
''', encoding='utf-8')
    return model, {'runtime': str(runtime.relative_to(stage)), 'binary': str(binary.relative_to(stage)),
                   'hexadecimal': str(hexadecimal.relative_to(stage)), 'binary_bytes': binary.stat().st_size,
                   'representation': 'typed FunctionalIR data evaluated by pure Lean BitVec operations'}
