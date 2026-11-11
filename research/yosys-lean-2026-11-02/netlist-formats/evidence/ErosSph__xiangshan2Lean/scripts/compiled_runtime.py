"""Generate inline Lean helpers with kernel-checked definitional equalities.

The baseline runtime is unchanged. Each helper copies its existing definition,
adds an inline attribute and proves equality to the original with rfl.
"""
from __future__ import annotations

import re
from pathlib import Path

from common import ROOT


def source():
    original = (ROOT / 'lean/Chisel2Lean/Functional.lean').read_text(encoding='utf-8')
    incremental = (ROOT / 'lean/Chisel2Lean/Incremental.lean').read_text(encoding='utf-8')
    names = {'arrayAt':'compiledAt', 'require':'compiledRequire', 'asBits':'asBitsCompiled',
             'asBool':'asBoolCompiled', 'nonzero':'nonzeroCompiled',
             'evaluateNode':'evaluateNodeCompiled', 'scheduleNode':'compiledSchedule'}
    def extract(text, start, end):
        value = text[text.index(start):text.index(end)].strip()
        for old, new in names.items():
            value = re.sub(r'\b'+old+r'\b', new, value)
        return '@[inline] ' + value + '\n'
    pieces = ['import Chisel2Lean.Incremental\nnamespace Chisel2Lean.Functional\n']
    for start, end in [('def arrayAt ', 'def require '), ('def require ', 'def Value.sort '),
                       ('def Value.asBits ', 'def Value.asBool '), ('def Value.asBool ', 'def Value.nonzero '),
                       ('def Value.nonzero ', 'def Value.toJson '), ('def evaluateNode ', 'structure Evaluation where')]:
        pieces.append(extract(original, start, end))
    pieces.append(extract(incremental, 'def scheduleNode ', 'structure IncrementalCache '))
    pieces.append('''
theorem compiledAt_eq (values : Array α) (index : Nat) :
    compiledAt values index = arrayAt values index := rfl
theorem compiledRequire_eq (condition : Bool) (message : String) :
    compiledRequire condition message = require condition message := rfl
theorem asBitsCompiled_eq (value : Value) (width : Nat) :
    value.asBitsCompiled width = value.asBits width := rfl
theorem asBoolCompiled_eq (value : Value) : value.asBoolCompiled = value.asBool := rfl
theorem nonzeroCompiled_eq (value : Value) : value.nonzeroCompiled = value.nonzero := rfl
theorem evaluateNodeCompiled_eq (inputs states values : Array Value) (domains : Array Bool) (node : Node) :
    evaluateNodeCompiled inputs states values domains node = evaluateNode inputs states values domains node := rfl
theorem compiledSchedule_eq (pending : Array UInt64) (index : Nat) :
    compiledSchedule pending index = scheduleNode pending index := rfl
#print axioms compiledAt_eq
#print axioms evaluateNodeCompiled_eq
#print axioms compiledSchedule_eq
end Chisel2Lean.Functional
''')
    return '\n'.join(pieces)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(source(), encoding='utf-8')
