"""Replay strict four-state JSONL inputs on an actual generated Lean model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from fourstate2lean import check_interface, digest, fresh_directory, write
from yosys_simulate import simulate_yosys


def load(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    base = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['format'] not in ('chisel2lean-fourstate-manifest-v1', 'chisel2lean-fourstate-manifest-v2',
                                  'chisel2lean-fourstate-manifest-v3', 'chisel2lean-fourstate-manifest-v4',
                                  'chisel2lean-fourstate-manifest-v5') or manifest['status'] != 'COMPILED':
        raise ValueError('Unsupported or incomplete four-state manifest')
    version = manifest['format'].rsplit('-', 1)[-1]
    expanded = version in ('v4', 'v5')
    required = ['mapping', 'ir'] + (['sources', 'elaboration', 'fourstate_ir'] if expanded else [])
    if any(not isinstance(manifest.get(key), str) or manifest[key] not in manifest.get('artifact_sha256', {}) for key in required):
        raise ValueError('Manifest required interface/provenance asset missing or unhashed')
    for name, expected in manifest['artifact_sha256'].items():
        path = (base / name).resolve()
        if not path.is_relative_to(base) or not path.is_file() or digest(path) != expected:
            raise ValueError('Four-state artifact hash mismatch: ' + name)
    for name in (manifest['mapping'], manifest['ir']):
        if name not in manifest['artifact_sha256']:
            raise ValueError('Manifest interface asset is not hashed')
    mapping = json.loads((base / manifest['mapping']).read_text(encoding='utf-8'))
    modern = version in ('v3', 'v4', 'v5')
    if modern and manifest.get('mode') not in ('combinational', 'sequential'):
        raise ValueError('Invalid four-state mode')
    sequential = manifest.get('mode') == 'sequential' if modern else version == 'v2'
    if sequential != (mapping.get('mode') == 'sequential') or (sequential and manifest.get('clock') != mapping.get('clock')):
        raise ValueError('Four-state manifest and port mapping mode mismatch')
    expected_mapping = 'chisel2lean-fourstate-ports-' + manifest['format'].rsplit('-', 1)[-1]
    if mapping.get('format') != expected_mapping or (modern and mapping.get('mode') != manifest.get('mode')):
        raise ValueError('Four-state manifest and port mapping version mismatch')
    if expanded:
        sources = json.loads((base / manifest['sources']).read_text(encoding='utf-8'))
        elaboration = json.loads((base / manifest['elaboration']).read_text(encoding='utf-8'))
        fourstate_ir = json.loads((base / manifest['fourstate_ir']).read_text(encoding='utf-8'))
        if version == 'v5':
            capabilities = fourstate_ir.get('capabilities')
            supported = {'net_initializer', 'packed_select', 'packed_vectors', 'reductions', 'replication',
                         'procedural_locals', 'preprocessing', 'async_posedge', 'unpacked_memory', 'bounded_events', 'immediate_assertions'}
            if (not isinstance(capabilities, list) or not capabilities or
                    any(not isinstance(c, str) or c not in supported for c in capabilities) or
                    len(capabilities) != len(set(capabilities))):
                raise ValueError('Invalid v5 capability inventory')
        if 'immediate_assertions' in fourstate_ir.get('capabilities', []):
            from fourstate_assertions import validate_mapping,FIELDS
            validate_mapping(mapping)
            sites=fourstate_ir.get('assertions')
            if ('bounded_events' not in fourstate_ir['capabilities'] or
                    [{k:v for k,v in a.items() if k not in FIELDS} for a in mapping['assertions']]!=sites):
                raise ValueError('Immediate assertion IR/observation contract mismatch')
            for a in sites:
                for key in ('enable_node','pass_node'):
                    if type(a[key]) is not int or not 0<=a[key]<len(fourstate_ir['nodes']) or fourstate_ir['nodes'][a[key]]['width']!=1:
                        raise ValueError('Invalid assertion IR flags')
        elif any('assertions' in x for x in (mapping,fourstate_ir)):
            raise ValueError('Assertion observations require immediate_assertions capability')
        asynchronous = 'async_posedge' in fourstate_ir.get('capabilities', [])
        if 'unpacked_memory' in fourstate_ir.get('capabilities', []):
            memories = fourstate_ir.get('memories')
            if not memories or memories != mapping.get('memories') or memories != elaboration.get('memories'):
                raise ValueError('Memory layout inventory mismatch')
            expected = {word for m in memories for word in m['words']}
            actual = {p['name'] for p in fourstate_ir['states'] if 'memory' in p}
            if expected != actual or len(expected) != sum(len(m['words']) for m in memories):
                raise ValueError('Memory state inventory mismatch')
        elif any('memories' in x for x in (mapping,fourstate_ir,elaboration)):
            raise ValueError('Memory metadata requires unpacked_memory capability')
        bounded = 'bounded_events' in fourstate_ir.get('capabilities', [])
        if bounded:
            from fourstate_bounded_events import validate_contract
            validate_contract(mapping)
            if (asynchronous or 'unpacked_memory' in fourstate_ir['capabilities'] or
                    any(x.get('events') != mapping['events'] for x in (manifest,fourstate_ir,elaboration))):
                raise ValueError('Bounded event contract mismatch or incompatible capability')
        elif mapping.get('monitors'):
            raise ValueError('Monitors require bounded_events capability')
        if asynchronous:
            from fourstate_events import validate_contract
            validate_contract(mapping)
            if manifest.get('events') != mapping['events'] or fourstate_ir.get('events') != mapping['events']:
                raise ValueError('Asynchronous event contract mismatch')
        elif not bounded and any('events' in x for x in (manifest, mapping, fourstate_ir)):
            raise ValueError('Event metadata requires explicit async_posedge capability')
        if (fourstate_ir.get('format') != 'chisel2lean-fourstate-ir-' + version or fourstate_ir.get('mode') != mapping['mode']
                or any(x.get('top') != mapping['top'] for x in (sources, elaboration, fourstate_ir))):
            raise ValueError('Expanded elaboration/source/IR identity mismatch')
        files = sources.get('files')
        if not isinstance(files, list) or not files or len(files) != len(fourstate_ir.get('sources', [])):
            raise ValueError('Expanded complete source list required')
        if len({x['snapshot'] for x in files}) != len(files) or [x['path'] for x in files] != fourstate_ir['sources']:
            raise ValueError('Expanded source list mismatch')
        for entry in files:
            if manifest['artifact_sha256'].get(entry['snapshot']) != entry['sha256']:
                raise ValueError('Expanded source snapshot missing or inconsistent')
        if 'preprocessing' in fourstate_ir.get('capabilities', []):
            key = manifest.get('preprocessing')
            if key not in manifest['artifact_sha256']:
                raise ValueError('Preprocessing receipt missing or unhashed')
            pp = json.loads((base / key).read_text(encoding='utf-8'))
            if pp != fourstate_ir.get('preprocessing') or pp.get('status') != 'PREPROCESSED':
                raise ValueError('Preprocessing receipt mismatch')
            used = [p for p in pp['files'] if p['role'] != 'admission-only']
            if {p['path'] for p in used} != {p['path'] for p in files}:
                raise ValueError('Preprocessing source inventory mismatch')
            for p in used:
                if manifest['artifact_sha256'].get(p['snapshot']) != p['sha256']:
                    raise ValueError('Preprocessed source hash mismatch')
        keys = ('name', 'rtl_path', 'width', 'signed', 'left', 'right')
        def state_set(items):
            rows = {p['name']: tuple(p[k] for k in keys) for p in items}
            if len(rows) != len(items):
                raise ValueError('Duplicate expanded state path')
            return rows
        if not (state_set(mapping.get('states', [])) == state_set(fourstate_ir.get('states', [])) == state_set(elaboration.get('states', []))):
            raise ValueError('Expanded register inventory mismatch')
        for port in mapping['ports']:
            if port.get('role') != 'clock' and port.get('bit_indices') != [port['right'] + (i if port['left'] >= port['right'] else -i) for i in range(port['width'])]:
                raise ValueError('Expanded declaration range/bit order mismatch')
        for state in mapping.get('states', []):
            if state.get('bit_indices') != [state['right'] + (i if state['left'] >= state['right'] else -i) for i in range(state['width'])]:
                raise ValueError('Expanded state declaration range/bit order mismatch')
    ir_path = base / manifest['ir']
    check_interface(json.loads(ir_path.read_text(encoding='utf-8')), mapping)
    return mapping, ir_path


def encode_trace(mapping, trace):
    ports = [p for p in mapping['ports'] if p['direction'] == 'input']
    if not isinstance(trace, list) or not trace:
        raise ValueError('Nonempty four-state trace required')
    result = []
    for row in trace:
        if not isinstance(row, dict) or set(row) != {'inputs'} or not isinstance(row['inputs'], dict) or set(row['inputs']) != {p['name'] for p in ports}:
            raise ValueError('Trace must supply exactly the original input ports')
        values = {}
        for p in ports:
            bits = row['inputs'][p['name']]
            if not isinstance(bits, str) or len(bits) != p['width'] or any(c not in '01xz' for c in bits):
                raise ValueError('Expected exact-width lowercase 01xz string: ' + p['name'])
            values[p['tag']] = int(''.join('1' if c in 'xz' else '0' for c in bits), 2)
            values[p['value']] = int(''.join('1' if c in '1z' else '0' for c in bits), 2)
        result.append({'inputs': values})
    return result


def decode(mapping, values):
    ports = [p for p in mapping['ports'] if p['direction'] == 'output']
    if set(values) != {p[key] for p in ports for key in ('tag', 'value')}:
        raise ValueError('Encoded output ports missing or extra')
    result = {}
    for p in ports:
        t, v = values[p['tag']], values[p['value']]
        if any(type(x) is not int or not 0 <= x < (1 << p['width']) for x in (t, v)):
            raise ValueError('Non-binary or out-of-width encoded output')
        result[p['name']] = ''.join('01xz'[(((t >> i) & 1) << 1) | ((v >> i) & 1)] for i in reversed(range(p['width'])))
    return result


def simulate(manifest_path, trace, evidence, initial=None):
    mapping, ir_path = load(manifest_path)
    if mapping.get('mode') == 'sequential':
        from fourstate_sequential import simulate as simulate_sequential
        return simulate_sequential(mapping, ir_path, trace, initial, evidence)
    if initial is not None:
        raise ValueError('Combinational replay does not accept --initial-file')
    encoded = encode_trace(mapping, trace)
    evidence = fresh_directory(evidence)
    write(evidence / 'fourstate-request.json', trace)
    code, rows = simulate_yosys(ir_path, encoded, {}, evidence / 'lean')
    if code or len(rows) != len(trace):
        raise RuntimeError('Lean execution failed or incomplete')
    result = []
    for i, row in enumerate(rows):
        if row['cycle'] != i or row['pre'] != row['post']:
            raise ValueError('Unexpected sampling/state behavior in combinational model')
        result.append({'sample': i, 'outputs': decode(mapping, row['pre'])})
    write(evidence / 'fourstate-result.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path); p.add_argument('--trace', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--initial-file', type=Path)
    a = p.parse_args()
    trace = [json.loads(line) for line in a.trace.read_text(encoding='utf-8').splitlines() if line.strip()]
    initial = json.loads(a.initial_file.read_text(encoding='utf-8')) if a.initial_file else None
    print(json.dumps(simulate(a.manifest, trace, a.evidence, initial), ensure_ascii=False))


if __name__ == '__main__':
    main()
