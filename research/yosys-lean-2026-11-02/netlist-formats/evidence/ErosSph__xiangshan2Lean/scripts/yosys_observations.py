"""Expose explicitly selected internal wires as read-only verification outputs."""
from __future__ import annotations

import json
from pathlib import Path
import re


def load(path: Path | None):
    if path is None:
        return []
    entries = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(entries, list) or not entries:
        raise ValueError('Observations must be a nonempty JSON list')
    names, wires = set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'name', 'wire', 'width'}:
            raise ValueError('Each observation requires exactly name, wire and width')
        name, wire, width = entry['name'], entry['wire'], entry['width']
        if not isinstance(name, str) or not re.fullmatch(r'c2l_observe_[A-Za-z0-9_]+', name):
            raise ValueError('Observation names must start with c2l_observe_')
        if not isinstance(wire, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.$]*', wire):
            raise ValueError('Observation wire must be an exact ordinary flattened wire name')
        if type(width) is not int or width < 1:
            raise ValueError('Observation width must be a positive integer')
        if name in names or wire in wires:
            raise ValueError('Duplicate observation name or wire')
        names.add(name)
        wires.add(wire)
    return entries


def plan(top, entries):
    if not entries:
        return ''
    # One selection/exposure per batch avoids walking a million-wire flattened
    # design separately for each tap. Names contain no selection wildcards.
    commands = ['cd \\' + top, 'select -assert-none w:c2l_observe_*',
                'tee -o /work/observation-original-ports.txt select -list x:*']
    for entry in entries:
        name, wire = entry['name'], entry['wire']
        commands.append('rename \\' + wire + ' \\' + name)
    # Default expose only adds an output. In particular, never use -cut,
    # -input or -evert here: those would change the observed circuit.
    # rename rejects missing sources and occupied destinations using exact
    # identifier lookup; no glob scan is needed for each individual source.
    return '; '.join(commands + ['expose w:c2l_observe_*', 'cd ..', 'select \\' + top])


def original_ports(path, top):
    prefix = top + '/'
    return {line.strip()[len(prefix):] for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip().startswith(prefix)}


def check_ports(ports, entries, source_ports=None):
    if entries and source_ports is None:
        raise ValueError('Missing original-port observation audit')
    for entry in entries:
        if entry['wire'] in source_ports:
            raise ValueError('Observation source is already a top-level port: ' + entry['wire'])
        port = ports.get(entry['name'])
        if port is None or port['direction'] != 'output' or len(port['bits']) != entry['width']:
            raise ValueError('Missing observation output or width mismatch: ' + entry['name'])
