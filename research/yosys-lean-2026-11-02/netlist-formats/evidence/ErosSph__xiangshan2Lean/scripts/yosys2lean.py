"""Convert fixed RTL through official Yosys FunctionalIR into executable Lean 4."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import gc
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
from collections import Counter

from bootstrap_yosys import INSTALL
from common import ROOT, lean_executable, run_logged
from functional_ir import emit_lean, expose_arbitrary, import_functional, import_functional_file, import_metadata, preflight
from functional_nodes import PackedNodes
from yosys_effects import attach_effects, expose_effects
from lean_records import access_paths
from lean_table import emit_table
from sv_stdio import normalize_stdio
from yosys_hierarchy import identity as hierarchy_identity, normalized_identity, functional_identity, reuse_checkpoint, save_checkpoint
from yosys_json import audit as stream_audit, expose as stream_expose
from yosys_expose import bulk_plan
from yosys_observations import (load as load_observations, plan as observation_plan,
                               check_ports as check_observation_ports, original_ports as original_observation_ports)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def write_ir(path, ir):
    if not isinstance(ir['nodes'], PackedNodes):
        return write(path, ir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        stream.write('{')
        for index, (key, value) in enumerate(ir.items()):
            stream.write((',' if index else '') + json.dumps(key) + ':')
            if key == 'nodes':
                stream.write('[')
                for serial, node in enumerate(value):
                    stream.write((',' if serial else '') + json.dumps(node, separators=(',', ':')))
                stream.write(']')
            else:
                json.dump(value, stream, separators=(',', ':'))
        stream.write('}\n')


# Hash code at import time. Long validations may continue while unrelated edits
# happen; their manifest must describe the code loaded by this process.
BRIDGE_FILES = {
    'reader_sha256': 'functional_ir.py', 'driver_sha256': 'yosys2lean.py',
    'chunk_emitter_sha256': 'lean_chunks.py', 'record_emitter_sha256': 'lean_records.py',
    'stdio_bridge_sha256': 'sv_stdio.py', 'effect_bridge_sha256': 'yosys_effects.py',
    'hierarchy_bridge_sha256': 'yosys_hierarchy.py', 'yosys_runner_sha256': 'yosys_runner.py',
    'json_bridge_sha256': 'yosys_json.py', 'json_reader_bootstrap_sha256': 'bootstrap_json_stream.py',
    'exposure_bridge_sha256': 'yosys_expose.py', 'functional_stream_sha256': 'functional_stream.py',
    'functional_node_storage_sha256': 'functional_nodes.py', 'table_emitter_sha256': 'lean_table.py',
    'observation_bridge_sha256': 'yosys_observations.py'}
BRIDGE_SOURCES = {filename: (ROOT / 'scripts' / filename).read_bytes() for filename in BRIDGE_FILES.values()}
BRIDGE_HASHES = {name: hashlib.sha256(BRIDGE_SOURCES[filename]).hexdigest() for name, filename in BRIDGE_FILES.items()}
BRIDGE_SOURCES['Functional.lean'] = (ROOT / 'lean/Chisel2Lean/Functional.lean').read_bytes()
BRIDGE_HASHES['table_runtime_sha256'] = hashlib.sha256(BRIDGE_SOURCES['Functional.lean']).hexdigest()


def stage_sources(source, stage, stdio_compat=False, virtual_root='/work'):
    compatibility = {'format': 'chisel2lean-stdio-compat-v1', 'files': {}, 'markers': {}}
    def adapt(original, target, relative):
        if not stdio_compat or b'$fwrite' not in target.read_bytes():
            return
        with target.open(encoding='utf-8', newline='') as stream:
            text, events = normalize_stdio(stream.read(), relative)
        if not events:
            return
        target.write_text(text, encoding='utf-8', newline='')
        compatibility['files'][relative] = {'original_sha256': digest(original), 'staged_sha256': digest(target), 'events': events}
        compatibility['markers'].update({e['marker']: e for e in events})
    if source.is_file():
        shutil.copy2(source, stage / 'design.v')
        adapt(source, stage / 'design.v', 'design.v')
        return virtual_root + '/design.v', {'design.v': digest(source)}, digest(source), compatibility
    if not source.is_dir():
        raise ValueError('RTL source must be a file or a directory of RTL files')
    files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix in {'.v', '.sv', '.vh', '.svh'})
    units, hashes, directories = [], {}, set()
    for original in files:
        if original.is_symlink():
            raise ValueError('Project RTL staging requires ordinary files: ' + str(original))
        relative = original.relative_to(source)
        dest = stage / 'project' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, dest)
        adapt(original, dest, relative.as_posix())
        hashes[relative.as_posix()] = digest(original)
        mapped = virtual_root + '/project/' + relative.as_posix()
        if original.suffix in {'.v', '.sv'}:
            units.append(mapped)
        directories.add(str(Path(mapped).parent).replace('\\', '/'))
    if not units:
        raise ValueError('Project directory contains no .v or .sv compilation units')
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"' if any(c.isspace() or c == '"' for c in value) else value
    arguments = ['-I' + d for d in sorted(directories)] + units
    (stage / 'project.f').write_text('\n'.join(quote(p) for p in arguments) + '\n', encoding='utf-8')
    fingerprint = hashlib.sha256(''.join(n + '\0' + h + '\n' for n,h in hashes.items()).encode()).hexdigest()
    return '-f ' + virtual_root + '/project.f', hashes, fingerprint, compatibility


def audit_hardware(module, clock, preserve_effects, clock_mode="single"):
    issues = []
    for name, cell in module['cells'].items():
        unit = {**module, 'cells': {name: cell}}
        try:
            preflight(unit, clock, preserve_effects=preserve_effects, clock_mode=clock_mode)
        except ValueError as error:
            issues.append({'cell': name, 'type': cell['type'], 'source': cell['attributes'].get('src', name),
                           'diagnostic': str(error)})
    return {'format': 'chisel2lean-hardware-audit-v1', 'clock': clock,
            'cell_types': dict(Counter(c['type'] for c in module['cells'].values())),
            'issues': issues, 'status': 'REQUIRES_SUPPORT' if issues else 'PREFLIGHT_PASS',
            'scope': 'Original normalized cells before clock lowering. PREFLIGHT_PASS is not a translation or equivalence claim.'}


def translate_yosys(source: Path, output: Path, top: str, namespace="Yosys.Design", clock=None,
                    evidence: Path | None = None, check=True, preserve_effects=False, clock_mode="single",
                    keep_hierarchy=False, stdio_compat=False, yosys_engine='wasm', hierarchy_checkpoint=None,
                    json_io='auto', yosys_transport='auto', flat_checkpoint=None, normalized_checkpoint=None,
                    functional_io='auto', functional_checkpoint=None, lean_backend='auto',
                    observations=None, merge_known_state=False):
    source, output = source.resolve(), output.resolve()
    observations = load_observations(observations)
    if clock_mode not in {"single", "global"}:
        raise ValueError("Clock mode must be single or global")
    if stdio_compat and not preserve_effects:
        raise ValueError('Standard-stream compatibility requires --effects preserve')
    if yosys_engine not in {'wasm', 'native'}:
        raise ValueError('Yosys engine must be wasm or native')
    if hierarchy_checkpoint and not keep_hierarchy:
        raise ValueError('Reusing a hierarchy checkpoint requires --keep-hierarchy')
    if json_io not in {'auto', 'stream', 'memory'}:
        raise ValueError('JSON IO must be auto, stream or memory')
    if functional_io not in {'auto', 'stream', 'memory'}:
        raise ValueError('FunctionalIR IO must be auto, stream or memory')
    if lean_backend not in {'auto', 'source', 'table'}:
        raise ValueError('Lean backend must be auto, source or table')
    if yosys_transport not in {'auto', 'json', 'rtlil'}:
        raise ValueError('Yosys transport must be auto, json or rtlil')
    rtlil_transport = yosys_transport == 'rtlil' or (yosys_transport == 'auto' and yosys_engine == 'native')
    if flat_checkpoint and (not rtlil_transport or hierarchy_checkpoint):
        raise ValueError('A flat checkpoint requires RTLIL transport and replaces the hierarchy checkpoint option')
    if normalized_checkpoint and (not rtlil_transport or hierarchy_checkpoint or flat_checkpoint):
        raise ValueError('A normalized checkpoint requires RTLIL transport and replaces the earlier checkpoint options')
    if functional_checkpoint and (not rtlil_transport or hierarchy_checkpoint or flat_checkpoint or normalized_checkpoint):
        raise ValueError('A FunctionalIR checkpoint requires RTLIL transport and replaces the earlier checkpoint options')
    if yosys_engine == 'native':
        from bootstrap_native_yosys import load_lock
        lock = load_lock(check_all=True)
    else:
        lock = json.loads((INSTALL / 'chisel2lean-lock.json').read_text(encoding='utf-8'))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ValueError("Top must be an ordinary Verilog module identifier")
    attempt = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8]
    log = (evidence or ROOT / "experiments/yosys-translation").resolve()/attempt
    stage=log/"artifacts"; stage.mkdir(parents=True)
    observation_files = [stage / 'observation-original-ports.txt'] if observations else []
    observation_names = tuple(path.name for path in observation_files)
    write(log / 'bridge-hashes.json', BRIDGE_HASHES)
    (log / 'bridge-source').mkdir()
    for filename, content in BRIDGE_SOURCES.items():
        (log / 'bridge-source' / filename).write_bytes(content)
    source_args, source_files, source_hash, stdio = stage_sources(source, stage, stdio_compat, '.' if yosys_engine == 'native' else '/work')
    write(stage / 'stdio-compatibility.json', stdio)
    runner=[sys.executable,str(ROOT/"scripts/yosys_runner.py"),"-Q","-T"]
    if yosys_engine == 'native':
        runner.insert(2, '--native')
    completed = []
    def progress(phase, status='RUNNING'):
        write(log / 'progress.json', {'phase': phase, 'status': status, 'completed': list(completed),
                                     'updated_at': dt.datetime.now(dt.timezone.utc).isoformat()})
    def run_yosys(script, name):
        if yosys_engine == 'native':
            script = script.replace('/work/', './')
        progress(name)
        script_file = stage / (name + '.ys')
        script_file.write_text(script + '\n', encoding='utf-8')
        script_hash = digest(script_file)
        write(log / name / 'script.json', {'path': str(script_file), 'sha256': script_hash})
        mapped = './' + script_file.name if yosys_engine == 'native' else '/work/' + script_file.name
        code = run_logged([*runner, '-s', mapped], log / name, cwd=stage, echo=False)
        if digest(script_file) != script_hash:
            raise ValueError('Yosys script changed during execution')
        if code == 0:
            completed.append(name)
        progress(name, 'PASS' if code == 0 else 'FAIL')
        return code
    frontend = 'read_slang --threads 1 --no-proc --no-synthesis-define --no-default-translate-off-format'
    if keep_hierarchy:
        frontend += ' --keep-hierarchy'
    # Flatten the selected root directly. Flattening every intermediate module
    # first holds another flattened copy at every hierarchy depth.
    first=f'{frontend} {source_args} --top {top}; hierarchy -check -top {top}; proc -noopt; flatten {top}; hierarchy -check -top {top}; opt_clean; memory_collect; write_json /work/before.json'
    reused_hierarchy = None
    signature = hierarchy_identity(top, source_files, stdio, yosys_engine, lock)
    flat_signature = {**signature, 'phase': 'flat',
                      'pipeline': 'root-only flatten v1; hierarchy -check -top; opt_clean; memory_collect'}
    if observations:
        flat_signature['read_only_observations'] = observations
        flat_signature['read_only_observation_pipeline'] = 'exact-rename-bulk-expose-v3'
    normalized_signature = normalized_identity(signature, clock_mode, preserve_effects, keep_hierarchy,
                                               merge_known_state, observations)
    exposure_pipeline = bulk_plan(top)
    functional_signature = functional_identity(normalized_signature, exposure_pipeline)
    reused_normalized = None
    reused_functional = None
    if hierarchy_checkpoint or flat_checkpoint or normalized_checkpoint or functional_checkpoint:
        progress('checkpoint-validation')
    if functional_checkpoint:
        reused_functional = reuse_checkpoint(Path(functional_checkpoint), stage / 'functional.smt2', functional_signature,
                                             required_companions=('before.json', 'named.json', 'lowered.json', *observation_names),
                                             artifact_kind='functional')
        for phase in ('yosys-prepare', 'yosys-functional', 'yosys-export'):
            write(log / phase / 'reused.json', reused_functional)
            completed.append(phase)
        original = json.loads(Path(functional_checkpoint).read_text(encoding='utf-8'))
        functional_producer = Path(original['producer']['result.json']['path']).parent
    if normalized_checkpoint:
        reused_normalized = reuse_checkpoint(Path(normalized_checkpoint), stage / 'unconstrained.il', normalized_signature,
                                             required_companions=('before.json', 'unconstrained.json', *observation_names))
        write(log / 'yosys-prepare/reused.json', reused_normalized)
        write(log / 'yosys-functional/reused.json', reused_normalized)
        completed.extend(('yosys-prepare', 'yosys-functional'))
        original = json.loads(Path(normalized_checkpoint).read_text(encoding='utf-8'))
        normalized_producer = Path(original['producer']['result.json']['path']).parent
    if keep_hierarchy and not flat_checkpoint and not normalized_checkpoint and not functional_checkpoint:
        # Complete the frontend before flattening in a fresh process. This avoids
        # keeping the elaborator's peak allocation alongside the full flat RTLIL.
        hierarchy = f'{frontend} {source_args} --top {top}; hierarchy -check -top {top}; proc -noopt; opt_clean; memory_collect; tee -o /work/hierarchy-stat.json stat -json; write_rtlil /work/hierarchy.il'
        producer = log / 'yosys-hierarchy'
        if hierarchy_checkpoint:
            reused_hierarchy = reuse_checkpoint(Path(hierarchy_checkpoint), stage / 'hierarchy.il', signature)
            write(log / 'yosys-hierarchy/reused.json', reused_hierarchy)
            original = json.loads(Path(hierarchy_checkpoint).read_text(encoding='utf-8'))
            producer = Path(original['producer']['result.json']['path']).parent
        else:
            if run_yosys(hierarchy, 'yosys-hierarchy'):
                raise RuntimeError('Yosys hierarchical RTL import failed; see ' + str(log / 'yosys-hierarchy'))
        save_checkpoint(stage / 'hierarchy-checkpoint.json', stage / 'hierarchy.il', signature,
                        producer)
        first = f'read_rtlil /work/hierarchy.il; hierarchy -check -top {top}; flatten {top}; hierarchy -check -top {top}; opt_clean; memory_collect; write_json /work/before.json'
    if observations:
        first = first.replace('opt_clean; memory_collect; write_json /work/before.json',
                              observation_plan(top, observations) + '; opt_clean; memory_collect; write_json /work/before.json')
    if rtlil_transport:
        first = first.replace('write_json /work/before.json', 'write_rtlil /work/before.il; write_json /work/before.json')
    reused_flat = None
    if flat_checkpoint:
        reused_flat = reuse_checkpoint(Path(flat_checkpoint), stage / 'before.il', flat_signature,
                                       required_companions=('before.json', *observation_names))
        write(log / 'yosys-prepare/reused.json', reused_flat)
        original = json.loads(Path(flat_checkpoint).read_text(encoding='utf-8'))
        flat_producer = Path(original['producer']['result.json']['path']).parent
    elif not normalized_checkpoint and not functional_checkpoint:
        if run_yosys(first, 'yosys-prepare'):
            raise RuntimeError('Yosys RTL normalization failed; see ' + str(log / 'yosys-prepare'))
        flat_producer = log / 'yosys-prepare'
    if rtlil_transport and not normalized_checkpoint and not functional_checkpoint:
        save_checkpoint(stage / 'flat-checkpoint.json', stage / 'before.il', flat_signature, flat_producer,
                        companions=[stage / 'before.json', *observation_files])
    modes = {}
    def streaming(path, phase):
        enabled = json_io == 'stream' or (json_io == 'auto' and path.stat().st_size > 64 * 1024 * 1024)
        modes[phase] = 'stream' if enabled else 'memory'
        return enabled
    progress('hardware-preflight')
    original_ports = original_observation_ports(observation_files[0], top) if observations else None
    if streaming(stage / 'before.json', 'preflight'):
        if observations:
            from bootstrap_json_stream import reader
            from yosys_json import ports
            check_observation_ports(ports(stage / 'before.json', top, reader()), observations, original_ports)
        clock, hardware, report = stream_audit(stage / 'before.json', top, clock, preserve_effects, clock_mode)
        write(stage / 'hardware-audit.json', report)
        if report['issues']:
            raise ValueError(report['issues'][0]['diagnostic'])
    else:
        data=json.loads((stage/"before.json").read_text(encoding='utf-8'))
        module=data['modules'][top]
        check_observation_ports(module['ports'], observations, original_ports)
        if clock is None:
            clock=next((n for n in ('clock','clk') if n in module['ports'] and module['ports'][n]['direction']=='input'),None)
        write(stage / 'hardware-audit.json', audit_hardware(module, clock, preserve_effects, clock_mode))
        hardware=preflight(module,clock,preserve_effects=preserve_effects,clock_mode=clock_mode)
        del module, data
    gc.collect()
    # Official lowering retains edge detection. Never silently abstract/gate a clock.
    second='read_json /work/before.json; clk2fflogic -nolower -nopeepopt; memory_nordff; opt_clean; setundef -undriven -anyseq; write_json /work/unconstrained.json'
    if merge_known_state:
        # Limit merging to lowered FFs. In particular, independent unknown
        # memories/arbitrary sources and repeated effect events must survive.
        second = second.replace('memory_nordff; opt_clean', 'memory_nordff; opt_merge -keepdc t:$ff; opt_clean')
    if preserve_effects:
        second = second.replace(' -nolower', '')
    if rtlil_transport:
        second = second.replace('read_json /work/before.json', 'read_rtlil /work/before.il')
        second = second.replace('write_json /work/unconstrained.json', 'write_rtlil /work/unconstrained.il; write_json /work/unconstrained.json')
    if not normalized_checkpoint and not functional_checkpoint:
        if run_yosys(second, 'yosys-functional'):
            raise RuntimeError("Yosys FunctionalIR conversion failed; see "+str(log/"yosys-functional"))
        normalized_producer = log / 'yosys-functional'
    if rtlil_transport and not functional_checkpoint:
        save_checkpoint(stage / 'normalized-checkpoint.json', stage / 'unconstrained.il', normalized_signature,
                        normalized_producer, companions=[stage / 'before.json', stage / 'unconstrained.json', *observation_files])
    def exposure_metadata(path, everted=False):
        if streaming(path, 'expose'):
            return stream_expose(path, None if everted else stage / 'exposed.json', top,
                                 preserve_effects, stdio['markers'], everted=everted)
        exposed = json.loads(path.read_text(encoding='utf-8'))
        lowered = exposed['modules'][top]
        effects = expose_effects(lowered, stdio['markers'], everted=everted) if preserve_effects else []
        arbitrary = expose_arbitrary(lowered, everted=everted)
        if not everted:
            write(stage / 'exposed.json', exposed)
        return import_metadata(lowered), effects, arbitrary
    if not rtlil_transport:
        metadata, effects, arbitrary = exposure_metadata(stage / 'unconstrained.json')
    gc.collect()
    final=f'read_json /work/exposed.json; check -assert; write_json /work/lowered.json; select {top}; write_functional_smt2 /work/functional.smt2'
    if rtlil_transport:
        final = ('read_rtlil /work/unconstrained.il; ' + exposure_pipeline + '; check -assert; '
                 'write_json /work/lowered.json; write_functional_smt2 /work/functional.smt2')
    if not functional_checkpoint:
        if run_yosys(final, 'yosys-export'):
            raise RuntimeError("Yosys transfer export failed; see "+str(log/"yosys-export"))
        functional_producer = log / 'yosys-export'
    if rtlil_transport:
        save_checkpoint(stage / 'functional-checkpoint.json', stage / 'functional.smt2', functional_signature,
                        functional_producer, companions=[stage / 'before.json', stage / 'named.json', stage / 'lowered.json', *observation_files],
                        artifact_kind='functional')
        shutil.copy2(stage / 'lowered.json', stage / 'exposed.json')
        progress('exposure-metadata')
        metadata, effects, arbitrary = exposure_metadata(stage / 'named.json', everted=True)
    progress('functional-import')
    functional_streaming = functional_io == 'stream' or (functional_io == 'auto' and (stage / 'functional.smt2').stat().st_size > 64 * 1024 * 1024)
    if functional_streaming:
        ir = import_functional_file(stage / 'functional.smt2', metadata, namespace, hardware['clock'], compact_nodes=True)
    else:
        ir = import_functional((stage / 'functional.smt2').read_text(), metadata, namespace, hardware['clock'])
    del metadata
    attach_effects(ir, effects)
    ir["hardware"]=hardware; ir["top"]=top
    if clock_mode == "global":
        ir['clock_mode'] = 'global'
    ir["arbitrary_inputs"]=arbitrary
    actual_backend = 'table' if lean_backend == 'table' or (lean_backend == 'auto' and (
        len(ir['nodes']) > 100000 or any(len(ir[kind]) > 8192 for kind in ('inputs', 'states', 'outputs')))) else 'source'
    ir['lean_backend'] = actual_backend
    if actual_backend == 'source':
        ir['lean_record_group_size'] = 16
        if any(len(ir[kind]) > ir['lean_record_group_size'] for kind in ('inputs', 'states', 'outputs')):
            ir['lean_field_paths'] = {kind: access_paths((f['field'] for f in ir[kind]), ir['lean_record_group_size'])
                                     for kind in ('inputs', 'states', 'outputs')}
    else:
        ir['lean_field_indices'] = {kind: {field['field']: index for index, field in enumerate(ir[kind])}
                                    for kind in ('inputs', 'states', 'outputs')}
    relative=Path(*namespace.split("."))
    lean=stage/relative.with_suffix(".lean"); lean.parent.mkdir(parents=True,exist_ok=True)
    ir_path=stage/relative.with_suffix(".ir.json")
    progress('typed-ir-write')
    write_ir(ir_path, ir)
    write(stage / 'functional-import-summary.json', {
        'status': 'PASS', 'stage': 'typed FunctionalIR import', 'nodes': len(ir['nodes']),
        'inputs': len(ir['inputs']), 'outputs': len(ir['outputs']), 'states': len(ir['states']),
        'node_storage': 'packed columns' if isinstance(ir['nodes'], PackedNodes) else 'Python dictionaries',
        'packed_column_bytes': ir['nodes'].storage_bytes() if isinstance(ir['nodes'], PackedNodes) else None,
        'ir': relative.with_suffix('.ir.json').as_posix(), 'lean_compilation': 'NOT_REACHED'})
    progress('lean-emission')
    table_info = None
    interface_path = None
    if actual_backend == 'table':
        lean, table_info = emit_table(ir, stage, runtime_source=BRIDGE_SOURCES['Functional.lean'])
        interface_path = stage / relative.with_suffix('.table.json')
        with interface_path.open('w', encoding='utf-8') as stream:
            json.dump({key: value for key, value in ir.items() if key != 'nodes'}, stream, separators=(',', ':'))
    else:
        lean.write_text(emit_lean(ir),encoding="utf-8")
    node_count, state_count = len(ir['nodes']), len(ir['states'])
    field_counts = {kind: len(ir[kind]) for kind in ('inputs', 'states', 'outputs')}
    node_storage = 'packed columns' if isinstance(ir['nodes'], PackedNodes) else 'Python dictionaries'
    record_group_size = ir.get('lean_record_group_size')
    del ir
    gc.collect()
    if check:
        env=os.environ.copy();env["LEAN_PATH"]=str(stage)
        if table_info:
            progress('lean-runtime-check')
            runtime = stage / table_info['runtime']
            if run_logged([lean_executable(), '-o', str(runtime.with_suffix('.olean')), str(runtime)],
                          log / 'lean-runtime-check', cwd=stage, env=env, echo=False):
                raise RuntimeError('Lean table runtime failed to compile; see ' + str(log / 'lean-runtime-check'))
        progress('lean-check')
        if run_logged([lean_executable(),"-o",str(lean.with_suffix(".olean")),str(lean)],log/"lean-check",cwd=stage,env=env,echo=False):
            raise RuntimeError("Generated Lean failed to compile; see "+str(log/"lean-check"))
        if table_info:
            prefix = 'C2L_TABLE_CHECK '
            audits = [json.loads(line[len(prefix):]) for line in (log / 'lean-check/stdout.log').read_text(encoding='utf-8').splitlines() if line.startswith(prefix)]
            if audits != [{'nodes': node_count, **field_counts}]:
                raise RuntimeError('Lean did not validate the complete encoded FunctionalIR table')
            write(log / 'table-data-check.json', {'status': 'PASS', **audits[0]})
        completed.append('lean-check')
    manifest={"format":"chisel2lean-yosys-manifest-v1","frontend":"yosys-functional","symbolic_parameters":False,
              "source":str(source),"source_sha256":source_hash,"source_files_sha256":source_files,"lean_version":"4.19.0","yosys_lock":lock,
              "namespace":namespace,"ir":relative.with_suffix(".ir.json").as_posix(),"lean":relative.with_suffix(".lean").as_posix(),"arbitrary_inputs":arbitrary,
              "yosys_engine":yosys_engine,
              "json_io":modes,
              "functional_io":'stream' if functional_streaming else 'memory',
              "node_storage":node_storage,
              "lean_backend":actual_backend,
              "table":table_info,
              "interface":interface_path.relative_to(stage).as_posix() if interface_path else None,
              "yosys_transport":'rtlil' if rtlil_transport else 'json',
              "frontend_keep_hierarchy":keep_hierarchy,
              "read_only_observations":observations,
              "merge_known_state":merge_known_state,
              "reused_hierarchy":reused_hierarchy,
              "reused_flat":reused_flat,
              "reused_normalized":reused_normalized,
              "reused_functional":reused_functional,
              "observation_exposure":'official bulk expose' if rtlil_transport else 'JSON cuts',
              "stdio_compatibility":stdio,
              "lean_checked":check,"hardware":hardware,"nodes":node_count,"evidence":str(log),
              "semantics":"Two-state completion, single positive-edge clock. clk2fflogic retains edges. Explicit initial state; unknown initial bits unconstrained. Data X/Z and undriven nets become explicit arbitrary inputs using setundef -anyseq. Reachable division by zero is rejected by execution domains. Not IEEE four-state simulation equivalence.",
              **BRIDGE_HASHES,
              "lean_emission":'typed FunctionalIR table' if table_info else '128-node typed chunks' if node_count > 4096 else 'single transfer function',
              "lean_chunk_storage":None if table_info else "balanced typed products",
              "lean_record_group_size":record_group_size,
              "artifact_sha256":{p.relative_to(stage).as_posix():digest(p) for p in (stage/"before.json",stage/"exposed.json",stage/"lowered.json",stage/"functional.smt2",lean,ir_path)}}
    if table_info:
        manifest['artifact_sha256'].update({p.relative_to(stage).as_posix(): digest(p) for p in
            [interface_path, *(stage / table_info[key] for key in ('runtime', 'binary', 'hexadecimal'))]})
    if 'stream' in modes.values():
        from bootstrap_json_stream import INSTALL as JSON_READER
        manifest['json_stream_reader'] = json.loads((JSON_READER / 'chisel2lean-lock.json').read_text(encoding='utf-8'))
    write(stage/"manifest.json",manifest)
    if clock_mode == 'global':
        manifest['semantics'] = 'Two-state explicit global sampling steps using official clk2fflogic, including multiple clocks, gated clocks, negative edges, latches, and supported asynchronous FF controls. Inputs must satisfy the negative-hold-time and stable-reset-release contract of clk2fflogic. This is not arbitrary-delay or IEEE four-state simulation equivalence.'
        write(stage/"manifest.json",manifest)
    if preserve_effects:
        manifest['effect_contract'] = 'Official clock-event lowering; assertion/assumption failures reject execution; cover and print events are observable. Additional observation outputs are not original hardware ports.'
        manifest['effects'] = effects
        write(stage/"manifest.json",manifest)
    output.mkdir(parents=True,exist_ok=True)
    for p in stage.rglob("*"):
        if p.is_file():
            dest=output/p.relative_to(stage);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    progress('translation', 'PASS')
    print(f'Generated {namespace}: {node_count} FunctionalIR nodes, {state_count} state fields ({actual_backend})',flush=True)
    return ir_path.relative_to(stage)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("--top",required=True)
    parser.add_argument("--out",type=Path,default=ROOT/"generated/yosys")
    parser.add_argument("--namespace",default="Yosys.Design")
    parser.add_argument("--clock")
    parser.add_argument("--evidence",type=Path)
    parser.add_argument("--effects", choices=["reject", "preserve"], default="reject")
    parser.add_argument("--clock-mode", choices=["single", "global"], default="single")
    parser.add_argument("--keep-hierarchy", action="store_true", help="Keep frontend hierarchy and flatten in a separate Yosys process")
    parser.add_argument('--stdio-compat', action='store_true', help='Preserve fixed stdout/stderr $fwrite via explicit observations')
    parser.add_argument('--yosys-engine', choices=['wasm', 'native'], default='wasm', help='Select a pinned Yosys runtime')
    parser.add_argument('--hierarchy-checkpoint', type=Path, help='Reuse an identical, hash-checked successful RTL import')
    parser.add_argument('--json-io', choices=['auto', 'stream', 'memory'], default='auto')
    parser.add_argument('--yosys-transport', choices=['auto', 'json', 'rtlil'], default='auto')
    parser.add_argument('--flat-checkpoint', type=Path, help='Resume after identical verified top-level flattening')
    parser.add_argument('--normalized-checkpoint', type=Path, help='Resume after identical verified clock and memory lowering')
    parser.add_argument('--functional-io', choices=['auto', 'stream', 'memory'], default='auto')
    parser.add_argument('--functional-checkpoint', type=Path, help='Resume Lean generation from an identical verified FunctionalIR export')
    parser.add_argument('--lean-backend', choices=['auto', 'source', 'table'], default='auto')
    parser.add_argument('--observations', type=Path, help='JSON list of exact internal wires to expose as read-only outputs')
    parser.add_argument('--merge-known-state', action='store_true', help='Run official opt_merge -keepdc before undefined inputs are introduced')
    parser.add_argument("--check",action=argparse.BooleanOptionalAction,default=True)
    args=parser.parse_args()
    translate_yosys(args.source,args.out,args.top,args.namespace,args.clock,args.evidence,args.check,args.effects=="preserve",args.clock_mode,args.keep_hierarchy,args.stdio_compat,args.yosys_engine,args.hierarchy_checkpoint,args.json_io,args.yosys_transport,args.flat_checkpoint,args.normalized_checkpoint,args.functional_io,args.functional_checkpoint,args.lean_backend,args.observations,args.merge_known_state)
    return 0


if __name__=="__main__":
    try: raise SystemExit(main())
    except (RuntimeError,ValueError) as e:
        print(str(e),file=sys.stderr);raise SystemExit(2)
