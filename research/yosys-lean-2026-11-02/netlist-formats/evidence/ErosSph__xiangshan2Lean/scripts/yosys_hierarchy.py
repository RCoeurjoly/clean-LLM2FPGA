"""Reuse a successful import only for identical RTL, preprocessing and tools."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

PIPELINE = ('read_slang --threads 1 --no-proc --no-synthesis-define '
            '--no-default-translate-off-format --keep-hierarchy; '
            'hierarchy -check; proc -noopt; opt_clean; memory_collect')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def identity(top, source_files, stdio, engine, lock):
    tool = {'engine': engine}
    for key in ('wasm_sha256', 'archive_sha256', 'yosys_version'):
        if key in lock:
            tool[key] = lock[key]
    return {'top': top, 'source_files_sha256': source_files, 'tool': tool,
            'stdio_staged_sha256': {name: item['staged_sha256'] for name, item in stdio['files'].items()},
            'pipeline': PIPELINE}


def normalized_identity(signature, clock_mode, preserve_effects, keep_hierarchy,
                        merge_known_state=False, observations=None):
    result = {**signature, 'phase': 'clock-normalized', 'clock_mode': clock_mode,
            'preserve_effects': preserve_effects, 'keep_hierarchy': keep_hierarchy,
            'pipeline': ('root-only flatten v1; hierarchy -check -top; opt_clean; memory_collect; '
                         'clk2fflogic ' + ('' if preserve_effects else '-nolower ') +
                         '-nopeepopt; memory_nordff; ' +
                         ('opt_merge -keepdc t:$ff; ' if merge_known_state else '') +
                         'opt_clean; setundef -undriven -anyseq')}
    if observations:
        result['read_only_observations'] = observations
        result['read_only_observation_pipeline'] = 'exact-rename-bulk-expose-v3'
    return result


def functional_identity(signature, exposure):
    return {**signature, 'phase': 'functional-export',
            'exposure_pipeline': exposure + '; check -assert; write_json; write_functional_smt2'}


def save_checkpoint(path, rtlil, expected, producer, companions=None, artifact_kind='rtlil'):
    if artifact_kind not in {'rtlil', 'functional'}:
        raise ValueError('Unsupported checkpoint artifact kind')
    result = json.loads((producer / 'result.json').read_text(encoding='utf-8'))
    if result['exit_code'] != 0 or not rtlil.is_file() or not rtlil.stat().st_size:
        raise ValueError('Hierarchy checkpoint requires a complete successful Yosys import')
    if rtlil.parent.resolve() != path.parent.resolve():
        raise ValueError('Keep a hierarchy checkpoint beside its RTLIL file')
    evidence = {name: {'path': str((producer / name).resolve()), 'sha256': digest(producer / name)}
                for name in ('command.json', 'result.json')}
    if (producer / 'script.json').is_file():
        script = json.loads((producer / 'script.json').read_text(encoding='utf-8'))
        if digest(Path(script['path'])) != script['sha256']:
            raise ValueError('Checkpoint Yosys script changed')
        evidence['script.ys'] = script
    report = {'format': 'chisel2lean-yosys-' + ('hierarchy' if artifact_kind == 'rtlil' else 'functional') + '-v1',
              'identity': expected, artifact_kind: {'name': rtlil.name, 'sha256': digest(rtlil)}, 'producer': evidence}
    if companions:
        if any(p.parent.resolve() != path.parent.resolve() for p in companions):
            raise ValueError('Keep companion artifacts beside their RTLIL checkpoint')
        report['companions'] = {p.name: digest(p) for p in companions}
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def reuse_checkpoint(path, target, expected, required_companions=(), artifact_kind='rtlil'):
    report = json.loads(path.read_text(encoding='utf-8'))
    checkpoint_format = 'chisel2lean-yosys-' + ('hierarchy' if artifact_kind == 'rtlil' else 'functional') + '-v1'
    if report.get('format') != checkpoint_format or report.get('identity') != expected:
        raise ValueError('Hierarchy checkpoint differs in RTL, top, preprocessing, pipeline or pinned Yosys')
    artifact = (path.parent / report[artifact_kind]['name']).resolve()
    if artifact.parent != path.parent.resolve() or digest(artifact) != report[artifact_kind]['sha256']:
        label = 'RTLIL' if artifact_kind == 'rtlil' else 'FunctionalIR'
        raise ValueError('Hierarchy checkpoint ' + label + ' hash mismatch')
    for item in report['producer'].values():
        if digest(Path(item['path'])) != item['sha256']:
            raise ValueError('Hierarchy checkpoint producer evidence changed')
    result = json.loads(Path(report['producer']['result.json']['path']).read_text(encoding='utf-8'))
    if result['exit_code'] != 0:
        raise ValueError('Hierarchy checkpoint producer did not succeed')
    companions = report.get('companions', {})
    if set(companions) != set(required_companions):
        raise ValueError('Checkpoint companion artifacts differ from the required stage')
    for name, expected_hash in companions.items():
        source = (path.parent / name).resolve()
        if source.parent != path.parent.resolve() or digest(source) != expected_hash:
            raise ValueError('Checkpoint companion artifact hash mismatch: ' + name)
    shutil.copy2(artifact, target)
    for name in companions:
        shutil.copy2(path.parent / name, target.parent / name)
    return {'path': str(path.resolve()), 'sha256': digest(path), artifact_kind + '_sha256': report[artifact_kind]['sha256']}
