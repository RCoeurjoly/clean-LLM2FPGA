#!/usr/bin/env python3
"""Insert whitespace after commas on long, unquoted generated SV lines."""

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
source = Path('/nix/store/c983mvq09inbcnsymkirpw3h9z3nisi8-tinystories-rtl/model.sv')
target = Path('/tmp/llm2fpga-full-hardware-20261102/model-formatted.sv')
original = source.read_bytes()
pieces, changed = [], []
for index, line in enumerate(original.splitlines(keepends=True), 1):
    if len(line) > 32768:
        # Commas are separate punctuation here. Reject contexts in which
        # adding a newline could change a token, macro, or comment.
        assert not any(token in line for token in [b'"', b'//', b'/*', b'*/', b'`', b'\\']), index
        assert b',' in line, index
        formatted = line.replace(b',', b',\n')
        assert formatted.replace(b',\n', b',') == line, index
        pieces.append(formatted)
        changed.append({'line': index, 'bytes': len(line), 'added_newlines': line.count(b',')})
    else:
        pieces.append(line)
target.write_bytes(b''.join(pieces))
report = {'source': str(source), 'target': str(target), 'changed': changed,
          'transformation': 'Only insert newlines immediately after commas on guarded lines; no tokens replaced',
          'source_sha256': hashlib.sha256(original).hexdigest(),
          'target_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
(root / 'sv-formatting.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
