"""Private execution-engine selection and verified replay preparation reuse."""
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def native_observer(build, model, observer):
    build, model = Path(build), Path(model)
    summary = json.loads((build / 'summary.json').read_bytes())
    if summary['status'] != 'EXACT_NATIVE_OBSERVER_PASS' or summary['samples'] != 1164:
        raise ValueError('Native observer requires completed exact execution validation')
    rows = summary['rows']
    if [r['fill'] for r in rows] != list('01xz') or any(
        r['execution']['exit_code'] != 0 or r['comparison'] != 'BYTE_IDENTICAL_TO_INTERPRETED_LEAN'
        for r in rows
    ):
        raise ValueError('Native observer validation incomplete')
    exe = build / 'run-machine-native'
    if str(exe) != summary['executable'] or digest(exe) != summary['executable_sha256']:
        raise ValueError('Native executable identity changed')
    if digest(observer) != summary['observer_sha256'] or digest(build / 'RunMachine.lean') != summary['observer_sha256']:
        raise ValueError('Native observer source differs')
    if digest(model / 'Chisel2Lean/Functional.lean') != summary['runtime_sha256']:
        raise ValueError('Native observer runtime differs')
    if digest(build / 'Chisel2Lean/Functional.lean') != summary['runtime_sha256']:
        raise ValueError('Native observer build runtime changed')
    return exe, dict(engine='lean-generated-c-native', executable_sha256=summary['executable_sha256'],
                     observer_sha256=summary['observer_sha256'], runtime_sha256=summary['runtime_sha256'],
                     validation=str(build / 'summary.json'))


def reuse_preparation(source, model, mapping, trace, request, scope, observer, target):
    """Reuse exact bit-mapping evidence, regenerate and compare every request value.

    This resumes a completed private preparation checkpoint. The public backend
    and its compiled circuit are checked anew; no public compiler gate is skipped.
    """
    from fourstate_unused_inputs import prove_unused
    from fourstate_scalable_bench import rails
    source, model, target = Path(source), Path(model), Path(target)
    old = json.loads((source.parent.parent / 'work' / source.name / 'request.json').read_bytes())
    for k in ('encode_label', 'backend_label', 'raw_reference_label', 'small_probe', 'probe_label', 'probe_top'):
        if old.get(k) != request.get(k):
            raise ValueError('Prepared replay source differs: ' + k)
    old_scope = json.loads((source / 'trace-scope.json').read_bytes())
    # Fill changes do not change the physical bit map. Every requested fill's
    # existing seed and every input frame are checked independently below.
    if {k: v for k, v in old_scope.items() if k != 'initial_fills'} != {k: v for k, v in scope.items() if k != 'initial_fills'}:
        raise ValueError('Prepared replay scope differs')
    if (source / 'RunMachine.lean').read_bytes() != Path(observer).read_bytes():
        raise ValueError('Prepared observer source differs')
    if json.loads((source / 'compile/execution-result.json').read_bytes())['exit_code'] != 0:
        raise ValueError('Prepared observer compilation failed')
    receipt = json.loads((source / 'state-map-receipt.json').read_bytes())
    manifest = json.loads((model / 'manifest.json').read_bytes())
    if digest(model / 'manifest.json') != receipt['model_manifest_sha256']:
        raise ValueError('Prepared model manifest differs')
    if receipt['lowered_artifact']['sha256'] != manifest['artifact_sha256']['lowered.json']:
        raise ValueError('Prepared lowered artifact differs')
    folder = source / 'request'
    verified_hashes = {}
    for name, key in [('plan.json', 'plan_sha256'), ('bank-witnesses.json', 'witness_sha256')]:
        if digest(folder / name) != receipt[key]:
            raise ValueError('Prepared mapping hash differs: ' + name)
        verified_hashes[name] = receipt[key]
    interface = model / (manifest.get('interface') or manifest['ir'])
    raw = interface.read_bytes()
    ir = json.loads(raw)
    unused, proof = prove_unused(model, manifest, ir, hashlib.sha256(raw).hexdigest())
    if receipt['unused_input_proof'] != proof or Path(receipt['binary']) != model / manifest['table']['binary']:
        raise ValueError('Prepared circuit proof differs')
    del raw
    witnesses = json.loads((folder / 'bank-witnesses.json').read_bytes())
    objects = {s['object_id']: s for s in mapping['states']}
    banks = {}
    expected_banks = {(s['object_id'], s[k]) for s in mapping['states'] for k in ('bank_tag', 'bank_value')}
    seen = set()
    for w in witnesses:
        pair = (w['object'], w['bank'])
        if pair not in expected_banks or pair in seen or not w['fields']:
            raise ValueError('Prepared bank coverage differs')
        seen.add(pair)
        obj = objects[w['object']]
        key = 'bank_tag' if w['bank'] == obj['bank_tag'] else 'bank_value'
        if w['names'] != [ir['states'][i]['name'] for i in w['fields']]:
            raise ValueError('Prepared bank field names differ')
        for i in w['fields']:
            if i in banks:
                raise ValueError('Prepared banks overlap')
            banks[i] = (obj, key)
    if seen != expected_banks or len(banks) != receipt['bank_fields'] or len(objects) != receipt['objects']:
        raise ValueError('Prepared bank mapping incomplete')
    names = {s['name'] for s in ir['inputs']}
    ports = [p for p in mapping['ports'] if p['direction'] == 'input']
    expected_names = {p[k] for p in ports for k in ('tag', 'value')} | {'exec_clk', 'step_valid', 'obs_object', 'obs_index'}
    if names != expected_names | unused or expected_names & unused:
        raise ValueError('Prepared input ABI differs')
    frames = []
    for row in trace:
        data = dict.fromkeys(names, 0)
        for p in ports:
            t, v = rails(row['inputs'][p['name']])
            data[p['tag']], data[p['value']] = int(t, 2), int(v, 2)
        frames.append(dict(inputs=data))
    files = ['plan.json', 'bank-witnesses.json']
    for fill in request.get('initial_fills', '01xz'):
        code = '01xz'.index(fill)
        initial = {}
        for i, s in enumerate(ir['states']):
            known, bank = s['initial'], banks.get(i)
            if s['sort']['kind'] == 'memory':
                value = [w['value'] for w in known['words']]
                if bank:
                    obj, key = bank
                    bit = int(code >= 2) if key == 'bank_tag' else code & 1
                    value = [((1 << obj['width']) - 1 if bit else 0) if j < obj['memory']['depth'] else 0 for j in range(len(value))]
                if any(v & w['mask'] != w['value'] for v, w in zip(value, known['words'])):
                    raise ValueError('Prepared memory seed violates constraints')
            else:
                value = known['value']
                if bank:
                    obj, key = bank
                    bit = int(code >= 2) if key == 'bank_tag' else code & 1
                    value = (1 << obj['width']) - 1 if bit else 0
                if value & known['mask'] != known['value']:
                    raise ValueError('Prepared scalar seed violates constraints')
            initial[s['name']] = value
        name = 'request-' + fill + '.json'
        request_bytes = (folder / name).read_bytes()
        if json.loads(request_bytes) != dict(initial=initial, trace=frames):
            raise ValueError('Prepared initial state or trace inputs differ')
        verified_hashes[name] = hashlib.sha256(request_bytes).hexdigest()
        files.append(name)
    target.mkdir()
    copied = []
    for name in files:
        before = verified_hashes[name]
        if digest(folder / name) != before:
            raise ValueError('Prepared file changed after validation')
        with (folder / name).open('rb') as src, (target / name).open('xb') as dst:
            shutil.copyfileobj(src, dst)
        if digest(folder / name) != before or digest(target / name) != before:
            raise ValueError('Prepared file changed during copy')
        copied.append(dict(path=name, sha256=before))
    return dict(receipt, reuse=dict(source=str(source), files=copied,
                validation='Same model/circuit/bit-map hashes; every selected initial value and input frame revalidated'))
