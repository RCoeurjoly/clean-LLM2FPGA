"""Validate native observations for closure reports; build acceptance is separate."""
import json
import math
from pathlib import Path
import re
from compiler.stages import digest
from compiler import measurements


def _number(value, label, *, integer=False, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value < 0 or (positive and value == 0) or
            (integer and type(value) is not int)):
        raise ValueError('invalid measurement units: '+label)
    return value


def load_measurement(directory: Path, expected_profile: str) -> dict:
    """Reconcile a producer observation with its specification and raw RSS trace.

    The caller must obtain expected_profile from the independently authenticated
    core profile. This function never promotes a successful process observation
    to a successful build, numerical comparison, or mapped-resource receipt.
    """
    root=Path(directory).resolve(strict=True)
    if not isinstance(expected_profile,str) or not re.fullmatch('[0-9a-f]{64}',expected_profile):
        raise ValueError('invalid expected profile identity')
    try:
        receipt=json.loads((root/'measurement.json').read_text())
        spec_path=root/'measurement-spec.json'
        spec=json.loads(spec_path.read_text())
        if not isinstance(receipt,dict) or not isinstance(spec,dict):
            raise ValueError('measurement receipt and spec must be objects')
        if spec.get('collector_sha256')!=digest(Path(measurements.__file__)):
            raise ValueError('measurement collector identity mismatch')
        if receipt.get('schema_version')!=1:
            raise ValueError('unsupported measurement schema')
        if receipt.get('profile_sha256')!=expected_profile or spec.get('profile_sha256')!=expected_profile:
            raise ValueError('measurement profile mismatch')
        if receipt.get('measurement_spec_sha256')!=digest(spec_path):
            raise ValueError('measurement spec identity mismatch')
        for section in ('input_hashes','tool_hashes'):
            identities=spec.get(section)
            if not isinstance(identities,dict) or not identities:
                raise ValueError('missing measurement dependency identities')
            for name,identity in identities.items():
                if not Path(name).is_absolute() or digest(Path(name))!=identity:
                    raise ValueError('measurement dependency changed: '+name)
        if receipt.get('inputs_authenticated') is not True:
            raise ValueError('measurement dependencies were not authenticated')
        if receipt.get('status') not in ('completed','failed','timeout'):
            raise ValueError('unsupported measurement status')
        code=receipt.get('exit_code')
        if code is not None and type(code) is not int:
            raise ValueError('invalid native exit code')
        if receipt['status']=='completed' and code!=0:
            raise ValueError('completed measurement requires zero exit')
        if receipt['status']=='failed' and code==0:
            raise ValueError('failed measurement has successful exit')
        if receipt.get('signal')!=(-code if code is not None and code<0 else None):
            raise ValueError('native signal and exit disagree')
        elapsed=_number(receipt.get('elapsed_seconds'),'seconds')
        interval=_number(receipt.get('sample_interval_seconds'),'sample interval',positive=True)
        if interval!=spec.get('sample_interval_seconds') or receipt.get('limits')!=spec.get('limits'):
            raise ValueError('measurement limits/spec mismatch')
        _number(spec['limits']['timeout_seconds'],'timeout seconds',positive=True)
        names=('stdout.log','stderr.log','memory-samples.jsonl')
        logs=receipt.get('raw_logs',{})
        for name in names:
            if name not in logs or Path(logs[name]).resolve()!=root/name or not (root/name).is_file():
                raise ValueError('missing or nonlocal raw log: '+name)
        rows=[json.loads(line) for line in (root/'memory-samples.jsonl').read_text().splitlines()]
        last=0
        for row in rows:
            stamp=_number(row.get('elapsed_seconds'),'sample seconds')
            if stamp<last or stamp>elapsed:raise ValueError('sample time outside observation')
            last=stamp
            _number(row.get('rss_kib'),'RSS KiB',integer=True)
            _number(row.get('processes'),'process count',integer=True,positive=True)
        if receipt.get('peak_memory_kib') is not None:
            _number(receipt['peak_memory_kib'],'peak RSS KiB',integer=True)
        _number(receipt.get('peak_processes'),'peak process count',integer=True)
        if (_number(receipt.get('memory_samples'),'sample count',integer=True)!=len(rows) or
                receipt.get('peak_memory_kib')!=max((row['rss_kib'] for row in rows),default=None) or
                receipt.get('peak_processes')!=max((row['processes'] for row in rows),default=0)):
            raise ValueError('measurement peak/count does not reconcile with raw samples')
        _number(receipt.get('sampling_errors'),'sampling errors',integer=True)
        if receipt.get('peak_memory_method')!='maximum sampled sum of process-tree Linux procfs RSS':
            raise ValueError('unsupported peak memory accounting method')
        evidence={str(root/name):digest(root/name) for name in ('measurement.json','measurement-spec.json',*names)}
        return {**receipt,'build_acceptance':False,'evidence_hashes':evidence}
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as error:
        raise ValueError('invalid measurement evidence: '+str(error)) from error


def pipeline_identity(profile) -> str:
    """Compare compiler programs/policies separately from per-model data.

    This fingerprint is not a core receipt. Report callers must independently
    authenticate each model's complete core and its full per-model profile.
    Compiler source and primitive-library paths use logical names so identical
    content remains comparable across isolated checkouts or Nix source roots.
    Tool executable roles and exact tool identities remain part of the program.
    """
    from compiler.stages import _identity
    from compiler.build_profile import CoreBuildProfile
    import hashlib
    if not isinstance(profile,CoreBuildProfile):
        raise ValueError('authenticated typed build profile required')
    document=profile.document()
    contract_bytes=profile.numerical_contract.read_bytes()
    if hashlib.sha256(contract_bytes).hexdigest()!=document['numerical_contract_sha256']:
        raise ValueError('numerical contract changed during profile comparison')
    contract=json.loads(contract_bytes)
    arithmetic=contract.get('arithmetic') if isinstance(contract,dict) else None
    if not isinstance(arithmetic,dict) or not arithmetic:
        raise ValueError('numerical arithmetic rules required for common profile comparison')
    dependencies=document.pop('dependency_hashes')
    contract_path=str(profile.numerical_contract)
    if dependencies.pop(contract_path,None)!=document.pop('numerical_contract_sha256'):
        raise ValueError('numerical contract lacks authenticated profile provenance')
    roots=[Path(name).parents[1] for name in dependencies
           if Path(name).parts[-2:]==('compiler','build_profile.py')]
    if len(roots)!=1:raise ValueError('compiler source root is ambiguous')
    source=roots[0];library=profile.calyx_library
    # A contract may be both the selected model input and a registered default.
    # Retain its shared policy slot in either role, excluding only input hashes.
    registry=json.loads((source/'compiler/frontend/dependencies.json').read_text())
    registered_contracts={source/name for name in registry['compilation_sources']
        if Path(name).parts[:2]==('compiler','contracts') and Path(name).suffix=='.json'}
    if profile.numerical_contract in registered_contracts:
        dependencies[contract_path]=hashlib.sha256(contract_bytes).hexdigest()
    normalized={}
    for name,identity in dependencies.items():
        path=Path(name)
        if path in registered_contracts:
            payload=path.read_bytes()
            if hashlib.sha256(payload).hexdigest()!=identity:
                raise ValueError('registered numerical contract changed')
            identity=_identity(_numerical_contract_policy(json.loads(payload)))
            key='source-contract-policy:'+str(path.relative_to(source))
        elif path.is_relative_to(source):key='source:'+str(path.relative_to(source))
        elif path.is_relative_to(library):key='primitive:'+str(path.relative_to(library))
        else:key='external:'+name
        if key in normalized:raise ValueError('common profile dependency name collision')
        normalized[key]=identity
    document['dependency_hashes']=normalized
    document['arithmetic']=arithmetic
    document['numerical_policy']=_numerical_contract_policy(contract)
    document['comparison_schema_version']=2
    return _identity(document)


def xc7_resource_usage(counts: dict) -> dict:
    """Account mapped primitives without promoting cell counts to physical fit.

    Distributed RAM and shift-register cells remain separate. Their physical
    LUT occupancy requires packing evidence; a primitive count is not that cost.
    """
    ordinary = {f'LUT{i}' for i in range(1, 7)}
    registers = {'FDRE', 'FDSE', 'FDCE', 'FDPE'}
    lutram = {'RAM32X1S', 'RAM32X1D', 'RAM64X1S', 'RAM64X1D',
              'RAM128X1S', 'RAM256X1S', 'RAM32M', 'RAM64M'}
    shifts = {'SRL16E', 'SRLC16E', 'SRLC32E'}
    known = ordinary | registers | lutram | shifts | {
        'CARRY4', 'DSP48E1', 'INV', 'MUXF7', 'MUXF8', 'RAMB18E1', 'RAMB36E1'}
    if not isinstance(counts, dict):
        raise ValueError('mapped primitive counts must be an object')
    for name, count in counts.items():
        if name not in known:
            raise ValueError('unsupported or unmapped primitive: ' + str(name))
        if type(count) is not int or count < 0:
            raise ValueError('primitive counts must be nonnegative integers')
    memories = {name: counts[name] for name in sorted(lutram) if counts.get(name, 0)}
    return {'ordinary_lut': sum(counts.get(name, 0) for name in ordinary),
            'lutram_cells': memories, 'lutram_luts': None if memories else 0,
            'shift_register_cells': {name: counts[name] for name in sorted(shifts)
                                     if counts.get(name, 0)},
            'registers': sum(counts.get(name, 0) for name in registers),
            'bram36_equivalent': counts.get('RAMB36E1', 0) + counts.get('RAMB18E1', 0) / 2,
            'dsp': counts.get('DSP48E1', 0), 'physical_fit_acceptance': False}


def load_core_evidence(core: Path, profile) -> dict:
    """Authenticate complete compilation before collecting report metadata.

    This evidence cannot authorize numerical, mapped-resource or physical claims.
    Each of those requires its own independently authenticated downstream receipt.
    """
    from compiler.build_profile import CoreBuildProfile
    from compiler.build_core import verify_core_receipt
    if not isinstance(profile, CoreBuildProfile):
        raise ValueError('authenticated typed build profile required')
    profile.validate()
    root = Path(core)
    if root.is_symlink() or (root / 'core.json').is_symlink():
        raise ValueError('invalid core evidence: symlinked receipt or directory')
    root = root.resolve()
    try:
        receipt = verify_core_receipt(root, profile)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid core evidence: ' + str(error)) from error
    program = pipeline_identity(profile)
    return {'core_directory': str(root),
            'core_identity_sha256': receipt['identity_sha256'],
            'profile_sha256': receipt['profile_sha256'],
            'pipeline_identity_sha256': program,
            'materialized_inputs_sha256': receipt['materialized_inputs_sha256'],
            'contexts': [record['context'] for record in receipt['graphs']],
            'verified_dependencies': len(receipt['dependency_hashes']),
            'core_receipt_sha256': digest(root / 'core.json'),
            'compilation_authenticated': True,
            'numerical_acceptance': False, 'resource_acceptance': False,
            'physical_acceptance': False}


def _numerical_contract_policy(contract: dict) -> dict:
    """Keep all contract policy fields; only declared model input hashes vary."""
    if (not isinstance(contract, dict) or type(contract.get('schema_version')) is not int
            or contract['schema_version'] != 1 or not isinstance(contract.get('arithmetic'), dict)
            or not contract['arithmetic']):
        raise ValueError('unsupported numerical contract policy')
    if 'inputs' in contract and not isinstance(contract['inputs'], dict):
        raise ValueError('invalid numerical contract input hashes')
    return {key: value for key, value in contract.items() if key != 'inputs'}


def fit_linear_trend(points, *, x_unit: str, y_unit: str) -> dict:
    """Descriptive ordinary least squares; never an extrapolation guarantee.

    Callers supply authenticated, equally scoped observations. Missing lanes
    must stay missing rather than becoming zero-valued samples.
    """
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise ValueError('trend requires at least three observations')
    if not all(isinstance(unit, str) and unit.strip() for unit in (x_unit, y_unit)):
        raise ValueError('explicit trend units required')
    pairs=[]
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('trend observations must be pairs')
        pairs.append(tuple(_number(v, 'trend observation') for v in point))
    xs, ys=zip(*pairs)
    mx=math.fsum(xs)/len(xs);my=math.fsum(ys)/len(ys)
    variance=math.fsum((x-mx)**2 for x in xs)
    if variance == 0:
        raise ValueError('trend requires distinct input values')
    slope=math.fsum((x-mx)*(y-my) for x,y in pairs)/variance
    intercept=my-slope*mx
    residuals=[y-(intercept+slope*x) for x,y in pairs]
    total=math.fsum((y-my)**2 for y in ys)
    result={'method':'ordinary least squares with intercept',
            'sample_count':len(pairs),'x_unit':x_unit,'y_unit':y_unit,
            'slope':slope,'intercept':intercept,'residuals':residuals,
            'r_squared':1-math.fsum(r*r for r in residuals)/total if total else None,
            'observed_x_range':[min(xs),max(xs)],'predictive_acceptance':False}
    if not all(math.isfinite(v) for v in [slope,intercept,*residuals]):
        raise ValueError('trend numerical range exceeded')
    return result

def _native_observation_source(measured_spec, original_specs):
    matches = []
    for name in original_specs & measured_spec['input_hashes'].keys():
        original = json.loads(Path(name).read_text())
        if (original['argv'] == measured_spec['argv'] and
                original['tool_hashes'] == measured_spec['tool_hashes']):
            matches.append(name)
    if len(matches) != 1:
        raise ValueError('native observation requires one executed original core stage')
    return matches[0]


def build_report(receipts: list[dict], baseline_dir: Path, output_dir: Path) -> dict:
    """Assemble five authenticated compilation lanes and scoped observations.

    Each lane supplies model, core, typed profile, snapshot_config and measurements.
    A measurement supplies directory and scope (capture/raw_import/native/whole_build).
    Optional mapping is a qualified final XC7 stage with core dependency provenance.
    Missing or failed observations remain explicit; this report never accepts RTL,
    physical fit or timing. Raw directories must remain available for revalidation.
    """
    import csv
    from scripts.pipeline.map_graph_core_xc7 import verify_mapped_checkpoint
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError('fresh report output directory required')
    models = ('1M', '3M', '8M', '28M', '33M')
    if (not isinstance(receipts, list) or len(receipts) != 5 or
            any(not isinstance(lane, dict) for lane in receipts) or
            {lane.get('model') for lane in receipts} != set(models)):
        raise ValueError('five unique family lanes required')
    baseline = Path(baseline_dir).resolve(strict=True)
    # Reports are downstream artifacts. Resolving aliases before this check
    # prevents a symlink from hiding writes inside authenticated input bundles.
    input_directories = [baseline]
    try:
        for lane in receipts:
            input_directories.append(Path(lane['core']).resolve())
            declarations = lane['measurements']
            if not isinstance(declarations, list):
                raise ValueError('measurement declarations must be a list')
            for declaration in declarations:
                input_directories.append(Path(declaration['directory']).resolve())
            if lane.get('mapping') is not None:
                input_directories.append(Path(lane['mapping']).resolve())
    except (KeyError, TypeError) as error:
        raise ValueError('invalid report input directory: '+str(error)) from error
    if any(output == directory or output.is_relative_to(directory)
           for directory in input_directories):
        raise ValueError('report output overlaps baseline or authenticated input directory')
    if str(baseline).startswith('/nix/store/'):
        raise ValueError('durable copied baseline required')
    baseline_files = sorted(p for p in baseline.rglob('*') if p.is_file())
    if (not baseline_files or any(p.is_symlink() for p in baseline.rglob('*')) or
            not (baseline/'stat.json').is_file() or not (baseline/'summary.json').is_file()):
        raise ValueError('complete nonsymlink baseline bundle required')
    baseline_hashes = {str(p.relative_to(baseline)): digest(p) for p in baseline_files}
    baseline_summary = json.loads((baseline/'summary.json').read_text())
    baseline_stat = json.loads((baseline/'stat.json').read_text())
    if not isinstance(baseline_summary.get('usage'), dict) or not isinstance(baseline_stat.get('num_cells_by_type'), dict):
        raise ValueError('baseline resource provenance required')
    evidence = {str(p): digest(p) for p in baseline_files}
    evidence[str(Path(__file__).resolve())] = digest(Path(__file__).resolve())
    rows = []
    programs = set()
    for model in models:
        lane = next(lane for lane in receipts if lane['model'] == model)
        try:
            core, profile = Path(lane['core']).resolve(), lane['profile']
            authenticated = load_core_evidence(core, profile)
            programs.add(authenticated['pipeline_identity_sha256'])
            config = Path(lane['snapshot_config']).resolve(strict=True)
            contract = json.loads(profile.numerical_contract.read_text())
            if digest(config) != contract['inputs']['snapshot']['config.json']:
                raise ValueError('snapshot config differs from authenticated numerical contract')
            document = json.loads(config.read_text())
            dimensions = {name: _number(document[name], name, integer=True, positive=True)
                          for name in ('hidden_size', 'num_heads', 'num_layers', 'vocab_size')}
            if dimensions['hidden_size'] % dimensions['num_heads']:
                raise ValueError('hidden dimension must divide into attention heads')
            evidence[str(config)] = digest(config)
            evidence[str(profile.numerical_contract)] = digest(profile.numerical_contract)
            for name in ('core.json', 'profile.json'):
                if (core/name).is_file():
                    evidence[str(core/name)] = digest(core/name)
            row = {'model': model, **dimensions, **authenticated,
                   'whole_build_wall_seconds': None, 'whole_build_peak_rss_kib': None,
                   'capture_wall_seconds': None, 'raw_import_wall_seconds': None,
                   'separate_native_wall_seconds_sum': None,
                   'maximum_separate_native_sampled_rss_kib': None,
                   'ordinary_lut': None, 'lutram_luts': None, 'registers': None,
                   'bram36_equivalent': None, 'dsp': None, 'routed_fmax_mhz': None,
                   'measurement_observations': [], 'final_resource_status': 'missing',
                   'numerical_rtl_status': 'not established', 'physical_status': 'not established'}
            groups = {scope: [] for scope in ('capture', 'raw_import', 'native', 'whole_build')}
            declarations = lane['measurements']
            if not isinstance(declarations, list):
                raise ValueError('measurement declarations must be a list')
            directories = set()
            for declaration in declarations:
                if not isinstance(declaration, dict) or declaration.get('scope') not in groups:
                    raise ValueError('explicit supported measurement scope required')
                directory = Path(declaration['directory']).resolve()
                if directory in directories:
                    raise ValueError('duplicate measurement directory')
                directories.add(directory)
                observation = load_measurement(directory, profile.identity_sha256)
                evidence.update(observation['evidence_hashes'])
                scope = declaration['scope']
                groups[scope].append(observation)
                row['measurement_observations'].append({
                    'scope': scope, 'directory': str(directory),
                    **{key: observation[key] for key in ('status', 'exit_code', 'signal',
                       'elapsed_seconds', 'peak_memory_kib', 'limits', 'diagnostic')}})
            native = [decl for decl in declarations if decl['scope'] == 'native']
            if native:
                try:
                    core_document = json.loads((core/'core.json').read_text())
                    core_dependencies = core_document['dependency_hashes']
                    original_specs = {name for name in core_dependencies if name.endswith('/spec.json')}
                except (OSError, KeyError, TypeError, ValueError) as error:
                    raise ValueError('native observations require authenticated core stage declarations') from error
                if not original_specs:
                    raise ValueError('native observations require declared core stages')
                observed_specs = set()
                for declaration in native:
                    measured_spec = json.loads((Path(declaration['directory'])/'measurement-spec.json').read_text())
                    original_path = _native_observation_source(measured_spec, original_specs)
                    if original_path in observed_specs:
                        raise ValueError('duplicate native core stage')
                    observed_specs.add(original_path)
                    if measured_spec['input_hashes'][original_path] != core_dependencies[original_path]:
                        raise ValueError('native observation source spec mismatch')
                    original = json.loads(Path(original_path).read_text())
                    if measured_spec['argv'] != original['argv'] or measured_spec['tool_hashes'] != original['tool_hashes']:
                        raise ValueError('native observation program differs from core stage')
                    observation = next(q for q in row['measurement_observations'] if q['directory'] == str(Path(declaration['directory']).resolve()))
                    if observation['status'] == 'completed':
                        for name in original['outputs']:
                            expected = core_dependencies.get(str(Path(original_path).parent/name))
                            if expected is None or digest(Path(measured_spec['cwd'])/name) != expected:
                                raise ValueError('native replay output differs from authenticated core')
                if observed_specs != original_specs:
                    raise ValueError('native observations do not cover all original core stages')
            for scope, observations in groups.items():
                if scope != 'native' and len(observations) > 1:
                    raise ValueError('one observation per capture/raw-import/whole-build scope required')
                if not observations or any(q['status'] != 'completed' for q in observations):
                    continue
                if scope == 'native':
                    row['separate_native_wall_seconds_sum'] = math.fsum(q['elapsed_seconds'] for q in observations)
                    row['maximum_separate_native_sampled_rss_kib'] = max(q['peak_memory_kib'] for q in observations if q['peak_memory_kib'] is not None) if any(q['peak_memory_kib'] is not None for q in observations) else None
                else:
                    row[scope+'_wall_seconds'] = observations[0]['elapsed_seconds']
                    if scope == 'whole_build':
                        row['whole_build_peak_rss_kib'] = observations[0]['peak_memory_kib']
            mapping = lane.get('mapping')
            if mapping is not None:
                mapping = Path(mapping).resolve(strict=True)
                dependencies = verify_mapped_checkpoint(mapping)
                spec = json.loads((mapping/'spec.json').read_text())
                if spec['name'] != 'graph-core-xc7-final':
                    raise ValueError('final mapped resource stage required')
                source = json.loads((core/'core.json').read_text())['dependency_hashes']
                if any(dependencies.get(name) != value for name, value in source.items()):
                    raise ValueError('final mapping lacks matching complete core provenance')
                utilization = json.loads((mapping/'serialized-checkpoint/utilization.json').read_text())
                counts = utilization['design']['num_cells_by_type']
                row.update(xc7_resource_usage(counts))
                row['final_resource_status'] = 'qualified mapped resources; physical fit open'
                evidence.update(dependencies)
                for name in ('spec.json', 'success.json', 'serialized-checkpoint/utilization.json'):
                    evidence[str(mapping/name)] = digest(mapping/name)
            rows.append(row)
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
            raise ValueError('invalid family lane '+model+': '+str(error)) from error
    if len(programs) != 1:
        raise ValueError('family compiler program/profile mismatch')
    metrics = {
        'whole_build_wall_seconds': 'seconds', 'whole_build_peak_rss_kib': 'KiB',
        'capture_wall_seconds': 'seconds', 'raw_import_wall_seconds': 'seconds',
        'separate_native_wall_seconds_sum': 'seconds',
        'maximum_separate_native_sampled_rss_kib': 'KiB',
        'ordinary_lut': 'cells', 'registers': 'cells', 'bram36_equivalent': 'BRAM36 equivalents', 'dsp': 'cells'}
    fits = {}
    for metric, unit in metrics.items():
        points = [(row['hidden_size'], row[metric]) for row in rows if row[metric] is not None]
        fits[metric] = (fit_linear_trend(points, x_unit='hidden dimensions', y_unit=unit)
                        if len(points) >= 3 else {'status': 'insufficient completed observations',
                                                  'sample_count': len(points), 'y_unit': unit})
    result = {'schema_version': 1, 'status': 'incomplete',
              'scope': 'Authenticated compilation and scoped native observations; no full RTL, physical or routed timing acceptance. Separate-stage sums/maxima are not whole-build measurements.',
              'pipeline_identity_sha256': programs.pop(),
              'baseline_directory': str(baseline), 'baseline_hashes': baseline_hashes,
              'baseline_usage': baseline_summary['usage'], 'rows': rows, 'fits': fits}
    # Authenticate all inputs before creating outputs. The empty plot explicitly
    # communicates absent measurements instead of drawing fabricated zeros.
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    output.mkdir(parents=True)
    for name, document in (('matrix.json', result), ('fits.json', fits), ('evidence-index.json', evidence)):
        (output/name).write_text(json.dumps(document, indent=2, allow_nan=False)+'\n')
    columns = ['model', 'hidden_size', 'num_heads', 'num_layers', 'vocab_size',
               'core_identity_sha256', 'pipeline_identity_sha256', *metrics,
               'lutram_luts', 'routed_fmax_mhz', 'final_resource_status']
    with (output/'matrix.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
    figure, axes = plt.subplots(4, 3, figsize=(13, 13))
    for axis, (metric, unit) in zip(axes.flat, metrics.items()):
        points = [(row['hidden_size'], row[metric]) for row in rows if row[metric] is not None]
        if points:
            xs, ys = zip(*points);axis.scatter(xs, ys)
            if len(points) >= 3:
                fit = fits[metric];axis.plot(xs, [fit['intercept']+fit['slope']*x for x in xs])
        else:
            axis.text(.5, .5, 'No qualified observations', ha='center', transform=axis.transAxes)
        axis.set_title(metric.replace('_', ' '), fontsize=9)
        axis.set_xlabel('Hidden dimensions');axis.set_ylabel(unit)
    for axis in list(axes.flat)[len(metrics):]:
        axis.set_visible(False)
    figure.suptitle('Scoped observations; physical and numerical closure open')
    figure.tight_layout();figure.savefig(output/'measurements.svg');figure.savefig(output/'measurements.png');plt.close(figure)
    return result
