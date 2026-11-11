"""Stream official Yosys JSON while retaining the exact netlist and bit IDs."""
from __future__ import annotations

from collections import Counter
import json

from bootstrap_json_stream import reader
from functional_ir import expose_arbitrary, initial_cell_metadata, import_metadata, preflight
from yosys_effects import expose_effects
from yosys_expose import cell_ids, write_plan


def ports(path, top, backend):
    with path.open('rb') as stream:
        return next(backend.items(stream, 'modules.' + top + '.ports'))


def audit(path, top, clock, preserve_effects, clock_mode):
    backend = reader()
    interface = ports(path, top, backend)
    if clock is None:
        clock = next((name for name in ('clock', 'clk') if name in interface
                      and interface[name]['direction'] == 'input'), None)
    types, issues = Counter(), []
    registers = memories = 0
    active_clock = None
    with path.open('rb') as stream:
        for name, cell in backend.kvitems(stream, 'modules.' + top + '.cells'):
            types[cell['type']] += 1
            try:
                result = preflight({'ports': interface, 'cells': {name: cell}}, clock,
                                   preserve_effects=preserve_effects, clock_mode=clock_mode)
                registers += result['register_cells']
                memories += result['memory_cells']
                active_clock = result['clock'] or active_clock
            except ValueError as error:
                issues.append({'cell': name, 'type': cell['type'], 'source': cell['attributes'].get('src', name),
                               'diagnostic': str(error)})
    hardware = {'register_cells': registers, 'memory_cells': memories, 'clock': active_clock}
    if clock_mode == 'global':
        hardware.update(clock=None, clock_mode='global')
    report = {'format': 'chisel2lean-hardware-audit-v1', 'clock': clock, 'cell_types': dict(types),
              'issues': issues, 'status': 'REQUIRES_SUPPORT' if issues else 'PREFLIGHT_PASS',
              'scope': 'Original normalized cells before clock lowering. PREFLIGHT_PASS is not a translation or equivalence claim.'}
    return clock, hardware, report


def expose(path, output, top, preserve_effects, stdio_markers, plan=None, everted=False):
    backend = reader()
    interface = ports(path, top, backend)
    selected, removed = {}, set()
    effect_types = {'$assert', '$assume', '$cover', '$print'} if preserve_effects else set()
    with path.open('rb') as stream:
        for name, cell in backend.kvitems(stream, 'modules.' + top + '.cells'):
            if cell['type'] in {'$ff', '$mem_v2', '$anyseq'} | effect_types:
                selected[name] = initial_cell_metadata(cell) if cell['type'] in {'$ff', '$mem_v2'} else cell
            if cell['type'] in {'$anyseq'} | effect_types:
                removed.add(name)
    wires = {}
    with path.open('rb') as stream:
        for name, wire in backend.kvitems(stream, 'modules.' + top + '.netnames'):
            if 'init' in wire['attributes'] or name.startswith(('c2l_effect_', 'c2l_undefined_')):
                wires[name] = wire
    module = {'ports': interface, 'cells': selected, 'netnames': wires}
    identifiers = cell_ids(selected)
    arbitrary_cells = [name for name, cell in selected.items() if cell['type'] == '$anyseq']
    original_ports = set(interface)
    effects = expose_effects(module, stdio_markers, everted=everted) if preserve_effects else []
    arbitrary = expose_arbitrary(module, everted=everted)
    additions = {name: wire for name, wire in wires.items() if name in interface and name not in original_ports}
    if output is not None:
        rewrite(path, output, top, interface, additions, removed, backend)
    if plan is not None:
        write_plan(plan, top, effects, arbitrary_cells, arbitrary, identifiers)
    return import_metadata(module), effects, arbitrary


def rewrite(source, output, top, interface, additions, removed, backend):
    """Copy JSON tokens; only replace ports, append wires, and remove exposed cells."""
    with source.open('rb') as stream, output.open('w', encoding='utf-8', newline='') as target:
        events = iter(backend.basic_parse(stream))
        chunks, size = [], 0
        def emit(value):
            nonlocal size
            chunks.append(value)
            size += len(value)
            if size >= 65536:
                target.write(''.join(chunks))
                chunks.clear()
                size = 0
        def serialized(value):
            return json.dumps(value, ensure_ascii=True, separators=(',', ':'))
        def skip(first):
            depth = int(first[0] in {'start_map', 'start_array'})
            while depth:
                event, _ = next(events)
                depth += (event in {'start_map', 'start_array'}) - (event in {'end_map', 'end_array'})
        def copy(first, path):
            event, value = first
            if path == ('modules', top, 'ports'):
                skip(first)
                emit(serialized(interface))
            elif event == 'start_map':
                emit('{')
                comma = ''
                while True:
                    key_event, key = next(events)
                    if key_event == 'end_map':
                        break
                    if key_event != 'map_key':
                        raise ValueError('Malformed JSON map in Yosys output')
                    child = next(events)
                    if path == ('modules', top, 'cells') and key in removed:
                        skip(child)
                        continue
                    emit(comma + serialized(key) + ':')
                    comma = ','
                    copy(child, path + (key,))
                if path == ('modules', top, 'netnames'):
                    for name, wire in additions.items():
                        emit(comma + serialized(name) + ':' + serialized(wire))
                        comma = ','
                emit('}')
            elif event == 'start_array':
                emit('[')
                comma = ''
                while True:
                    child = next(events)
                    if child[0] == 'end_array':
                        break
                    emit(comma)
                    comma = ','
                    copy(child, path + ('[]',))
                emit(']')
            elif event in {'string', 'boolean', 'null'}:
                emit(serialized(value))
            elif event == 'number' and isinstance(value, int):
                emit(str(value))
            else:
                raise ValueError('Unsupported JSON token in locked Yosys output: ' + event)
        copy(next(events), ())
        if next(events, None) is not None:
            raise ValueError('Extra JSON root in Yosys output')
        target.write(''.join(chunks))
