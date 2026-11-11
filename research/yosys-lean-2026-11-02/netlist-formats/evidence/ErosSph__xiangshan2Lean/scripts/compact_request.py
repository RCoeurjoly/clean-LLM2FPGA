"""Record every physical input while keeping fixed arbitrary choices once."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from yosys_hierarchy import digest


def write_compact(template: Path, output: Path, names: list[str], trace, audit_every=0):
    if len(names) != len(set(names)):
        raise ValueError('Duplicate physical input name')
    with template.open('rb') as stream:
        initial = json.loads(stream.readline())['initial']
        defaults = json.loads(stream.readline())['inputs']
    if not set(names) <= defaults.keys():
        raise ValueError('Physical input missing in the checked template')
    if any(value != 0 for name,value in defaults.items() if name not in names):
        raise ValueError('Expected the explicit zero arbitrary-input experiment')
    count = 0
    with output.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(json.dumps({'initial':initial,'input_defaults':defaults,'input_order':names,
                                 'audit_every':audit_every},separators=(',',':'))+'\n')
        for values in trace:
            if set(values) != set(names):
                raise ValueError('Missing/extra physical inputs at sample '+str(count))
            stream.write(json.dumps({'inputs':[int(values[n]) for n in names]},separators=(',',':'))+'\n')
            count += 1
    return {'samples':count,'request_sha256':digest(output),'template_sha256':digest(template),
            'format':'physical-vector-with-explicit-defaults-v1','audit_every':audit_every}


def csv_inputs(path):
    with path.open(encoding='utf-8',newline='') as stream:
        for i,row in enumerate(csv.DictReader(stream)):
            if int(row.pop('sample')) != i:
                raise ValueError('Missing/reordered RTL input sample')
            yield {name:int(value,0) for name,value in row.items()}
