"""Validate the exact native ABI supported by the owned on-chip board shell.

This validates structured interface data; it never parses or edits emitted RTL.
The current physical shell is deliberately scoped to the fixed 1M geometry.
"""


def validate_onchip_interface(document):
    expected_ports = [dict(name=name, direction=direction, width=1)
        for name, direction in [('clk', 'input'), ('reset', 'input'),
                                ('go', 'input'), ('done', 'output')]]
    if (not isinstance(document, dict) or
        set(document) != {'schema_version', 'entrypoint', 'ports', 'memories'} or
        type(document['schema_version']) is not int or document['schema_version'] != 1 or
        document['entrypoint'] != 'main' or not isinstance(document['ports'], list) or
        any(not isinstance(port, dict) or set(port) != {'name', 'direction', 'width'}
            or type(port['width']) is not int for port in document['ports']) or
        document['ports'] != expected_ports or
        not isinstance(document['memories'], list) or len(document['memories']) != 3):
        raise ValueError('unsupported native on-chip entry interface')
    geometry = {'prompt': (4, 2, 'lutram'), 'generated_tokens': (16, 4, 'lutram'),
                'last_logits': (50257, 16, 'bram')}
    seen = set()
    for memory in document['memories']:
        if (not isinstance(memory, dict) or set(memory) != {
            'runtime_port', 'cell', 'component', 'width', 'depth', 'address_width',
            'implementation', 'read_latency', 'writable', 'ports'}):
            raise ValueError('unsupported runtime memory declaration')
        role = memory['runtime_port']
        if not isinstance(role, str) or role not in geometry or role in seen:
            raise ValueError('missing, duplicate or unknown runtime memory role')
        seen.add(role)
        depth, addr, storage = geometry[role]
        if (memory['cell'] != role or memory['component'] != 'main' or
            any(type(memory[key]) is not int for key in
                ('width', 'depth', 'address_width', 'read_latency')) or
            memory['width'] != 64 or memory['depth'] != depth or
            memory['address_width'] != addr or memory['implementation'] != storage or
            memory['read_latency'] != 2 or memory['writable'] is not True):
            raise ValueError('unsupported native runtime memory contract: ' + role)
        expected = {name: dict(role=name, name=role+'_'+name,
                              direction=direction, width=width)
            for name, direction, width in [('addr0', 'output', addr), ('clk', 'output', 1),
                ('reset', 'output', 1), ('content_en', 'output', 1), ('write_en', 'output', 1),
                ('write_data', 'output', 64), ('read_data', 'input', 64), ('done', 'input', 1)]}
        ports = memory['ports']
        if (not isinstance(ports, list) or len(ports) != len(expected) or
            any(not isinstance(port, dict) or set(port) != {'role', 'name', 'direction', 'width'}
                or not isinstance(port['role'], str) or type(port['width']) is not int
                for port in ports) or
            {port['role']: port for port in ports} != expected):
            raise ValueError('unsupported native runtime memory ports: ' + role)
