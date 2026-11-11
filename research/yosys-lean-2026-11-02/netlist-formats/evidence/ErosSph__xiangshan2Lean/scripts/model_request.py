"""Prepare an explicit zero-completion experiment for a newly checked model."""
from __future__ import annotations

import json
from pathlib import Path

from server_bundle import read
from yosys_hierarchy import digest


def frame(fields, initial):
    result = {}
    for field in fields:
        sort = field['sort']
        known = field.get('initial') if initial else None
        if initial and known is None:
            raise ValueError('Missing initial-state constraints: ' + field['name'])
        if sort['kind'] == 'bits':
            value = known['value'] if initial else 0
            if not 0 <= value < 1 << sort['width']:
                raise ValueError('Out-of-width initial value')
        elif sort['kind'] == 'boolean':
            value = bool(known['value']) if initial else False
        elif sort['kind'] == 'memory':
            count = 1 << sort['address_width']
            if initial and len(known['words']) != count:
                raise ValueError('Memory initial constraints have the wrong depth')
            value = [word['value'] for word in known['words']] if initial else [0] * count
            if any(not 0 <= word < 1 << sort['width'] for word in value):
                raise ValueError('Out-of-width memory initial value')
        else:
            raise ValueError('Unsupported input/state sort')
        if field['name'] in result:
            raise ValueError('Duplicate frame field')
        result[field['name']] = value
    return result


def write_model_request(model, output, names, trace, audit_every=256, observe_state=()):
    model = model.resolve()
    manifest = read(model / 'manifest.json')
    if not manifest.get('lean_checked') or manifest.get('lean_backend') != 'table':
        raise ValueError('Expected a fully checked Lean table model')
    interface = (model / manifest['interface']).resolve()
    if not interface.is_relative_to(model) or digest(interface) != manifest['artifact_sha256'][manifest['interface']]:
        raise ValueError('Model interface changed')
    metadata = read(interface)
    if metadata.get('clock') is not None:
        raise ValueError('Program replay requires explicit global clock sampling')
    defaults = frame(metadata['inputs'], False)
    initial = frame(metadata['states'], True)
    state_names = {field['name'] for field in metadata['states']}
    if len(names) != len(set(names)) or not set(names) <= defaults.keys():
        raise ValueError('Physical input names do not match the checked model')
    if len(observe_state) != len(set(observe_state)) or not set(observe_state) <= state_names:
        raise ValueError('Unknown/duplicate requested state observation')
    count = 0
    input_sorts = {field['name']: field['sort'] for field in metadata['inputs']}
    with output.open('x', encoding='utf-8', newline='\n') as stream:
        header = {'initial': initial, 'input_defaults': defaults, 'input_order': names,
                  'audit_every': audit_every, 'observe_state': list(observe_state),
                  'experiment': 'Unknown bits and arbitrary inputs explicitly chosen zero; all known initial constraints retained.'}
        stream.write(json.dumps(header, separators=(',', ':'))+'\n')
        for values in trace:
            if set(values) != set(names):
                raise ValueError('Incomplete physical input sample ' + str(count))
            for name in names:
                sort = input_sorts[name]
                if sort['kind'] != 'bits' or type(values[name]) not in (int, bool) or not 0 <= values[name] < 1 << sort['width']:
                    raise ValueError('Out-of-width physical input at sample '+str(count)+': '+name)
            stream.write(json.dumps({'inputs': [int(values[name]) for name in names]}, separators=(',', ':'))+'\n')
            count += 1
    return {'samples': count, 'request_sha256': digest(output), 'model_manifest_sha256': digest(model / 'manifest.json'),
            'interface_sha256': digest(interface), 'unknown_choice': 0, 'arbitrary_choice': 0,
            'initial_state_fields': len(initial), 'input_fields': len(defaults),
            'state_observations': list(observe_state), 'audit_every': audit_every}
