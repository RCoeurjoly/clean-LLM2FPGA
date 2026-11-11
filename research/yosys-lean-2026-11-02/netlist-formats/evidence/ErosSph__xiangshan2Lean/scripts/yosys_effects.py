"""Preserve officially lowered checks and print events as explicit observations."""
from __future__ import annotations

from functional_ir import number
from sv_stdio import restore_stdio


def expose_effects(module, stdio_markers=None, everted=False):
    effects = []
    for name in [name for name, cell in module['cells'].items() if cell['type'] in {'$assert', '$assume', '$cover', '$print'}]:
        cell = module['cells'][name]
        kind = cell['type'].removeprefix('$')
        if kind not in {'assert', 'assume', 'cover', 'print'}:
            continue
        parameters, connections = cell['parameters'], cell['connections']
        source = cell['attributes'].get('src', name)
        if number(parameters.get('TRG_ENABLE', 0)):
            raise ValueError(source + ': print trigger was not lowered to the global sampling clock')
        effect = {'cell': name, 'kind': kind, 'source': source,
                  'format': parameters.get('FORMAT', ''), 'ports': {}}
        if 'PRIORITY' in parameters:
            # Preserve the producer's ordering metadata; indices are local IDs.
            effect['priority_bits'] = parameters['PRIORITY']
        for role, port in [('enable', 'EN'), ('condition', 'A'), ('arguments', 'ARGS')]:
            bits = connections.get(port)
            if not bits:
                continue
            if role in {'enable', 'condition'} and len(bits) != 1:
                raise ValueError(source + ': effect predicate must be one bit')
            output = name + '_' + port if everted else f'c2l_effect_{len(effects)}_{role}'
            if output in module['ports'] or output in module['netnames']:
                raise ValueError('Reserved effect observation collides with source: ' + output)
            module['ports'][output] = {'direction': 'output', 'bits': bits}
            module['netnames'][output] = {'hide_name': 0, 'bits': bits, 'attributes': {'src': source}}
            effect['ports'][role] = output
        if 'enable' not in effect['ports'] or (kind != 'print' and 'condition' not in effect['ports']):
            raise ValueError(source + ': malformed lowered effect cell')
        effect['argument_width'] = len(connections.get('ARGS', []))
        restore_stdio(effect, stdio_markers or {})
        effects.append(effect)
        del module['cells'][name]
    return effects


def attach_effects(ir, effects):
    if not effects:
        return
    outputs = {f['name']: f for f in ir['outputs']}
    for serial, effect in enumerate(effects):
        effect['index'] = serial
        effect['fields'] = {role: outputs[name]['field'] for role, name in effect['ports'].items()}
        for role, name in effect['ports'].items():
            field = outputs[name]
            field['effect'] = serial
            field['effect_role'] = role
            field['effect_enable_node'] = outputs[effect['ports']['enable']]['node']
    ir['effects'] = effects
