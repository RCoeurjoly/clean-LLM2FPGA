"""Run the selected model-to-route commands with fresh generated inputs."""
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import time

import build

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / 'pipeline/flow.json'


def check_generated_inputs(directory, expected):
    """Missing or changed intermediates must never fall back to archived files."""
    for name, identity in expected.items():
        path = directory / name
        if build.digest(path) != identity:
            raise ValueError('Generated input/output differs from the recorded build: ' + str(path))


def namespace(work, flow, active=None):
    """Expose only this run's model outputs at the original compiler paths."""
    command = build.isolation(work)
    command += ['--tmpfs', '/nix/store']
    for path in json.loads((ROOT / 'toolchain/paths.json').read_text()):
        command += ['--ro-bind', path, path]
    command += ['--ro-bind', str(work / 'tree'), flow['original_root'],
                '--ro-bind', str(ROOT / 'source/upstream'), flow['source_store_path'],
                '--bind' if active == 'core' else '--ro-bind',
                str(work / 'core'), flow['core_store_path']]
    if active and active != 'core':
        command += ['--bind', str(work / 'tree' / active),
                    flow['original_root'] + '/' + active,
                    '--chdir', flow['original_root'] + '/' + active]
    else:
        command += ['--chdir', '/tmp']
    for key, value in {'PATH': '/usr/bin:/bin', 'OMP_NUM_THREADS': '1',
                       'OPENBLAS_NUM_THREADS': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                       'HOME': '/tmp', 'XDG_CACHE_HOME': '/tmp/cache'}.items():
        command += ['--setenv', key, value]
    return command


def require_runtime_paths(paths):
    for path in paths:
        if not Path(path).exists():
            raise FileNotFoundError('Missing pinned runtime input; import the toolchain archive: ' + path)


def preflight(flow):
    require_runtime_paths(json.loads((ROOT / 'toolchain/paths.json').read_text()))
    environment = json.loads((ROOT / 'pipeline/bitstream.json').read_text())
    require_runtime_paths([environment['PRJXRAY_DB_DIR'] + '/xc7k480tffg1156-1/part.yaml'])
    for tool in ('fasm2frames', 'xc7frames2bit', 'bitread'):
        if shutil.which(tool, path=environment['PATH']) is None:
            raise FileNotFoundError('Missing bitstream tool: ' + tool)
    identities = {}
    for stage in flow['stages']:
        identities.update(stage['tool_hashes'])
    identities.update(flow['core']['tool_hashes'])
    check_generated_inputs(Path('/'), identities)
    # The host C++ compiler and headers are needed for the two Yosys plugins.
    check_generated_inputs(Path('/'), json.loads((ROOT / 'toolchain/host-files.json').read_text()))
    print(f'Verified {len(identities)} pinned tool files and host compiler inputs', flush=True)


def execute(work, flow, name, spec, directory, active):
    command = namespace(work, flow, active)
    for key, value in spec.get('environment', {}).items():
        command += ['--setenv', key, value]
    command += spec['argv']
    record = {'name': name, 'command': command, 'status': 'running'}
    receipt = directory / 'build-result.json'
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    started = time.monotonic()
    print('Building ' + name, flush=True)
    try:
        with (directory / 'build.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                           timeout=spec['timeout_seconds'], check=True)
        check_generated_inputs(directory, spec['expected_outputs'])
        record.update(status='matched', output_hashes=spec['expected_outputs'])
    except Exception as error:
        record.update(status='failed', error=str(error))
        raise
    finally:
        record['seconds'] = time.monotonic() - started
        receipt.write_text(json.dumps(record, indent=2) + '\n')


def run(output):
    flow = json.loads(FLOW.read_text())
    preflight(flow)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'core').mkdir()
    shutil.copytree(ROOT / 'snapshot', output / 'tree')
    # Create every generated directory before exposing the tree read-only.
    # There are no archived intermediates in snapshot/.
    for stage in flow['stages']:
        directory = output / 'tree' / stage['directory']
        directory.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'flow_sha256': build.digest(FLOW),
              'expected_bitstream_sha256': build.EXPECTED,
              'starts_from': 'authenticated model checkpoint/package and PyTorch computation',
              'hardware_programmed': False}
    receipt = output / 'result.json'
    receipt.write_text(json.dumps(report, indent=2) + '\n')
    try:
        subprocess.run(namespace(output, flow) + ['/bin/true'], check=True)
        execute(output, flow, 'pytorch-core', flow['core'], output / 'core', 'core')
        for stage in flow['stages']:
            check_generated_inputs(output, stage['generated_inputs'])
            directory = output / 'tree' / stage['directory']
            if stage.get('stage_model_images'):
                for image in sorted((output / 'core').glob('input-*.hex')):
                    shutil.copyfile(image, directory / image.name)
            if stage.get('decompress_input'):
                with gzip.open(output / stage['decompress_input'], 'rb') as source:
                    with (directory / 'native-input.json').open('wb') as target:
                        shutil.copyfileobj(source, target)
            execute(output, flow, stage['name'], stage, directory, stage['directory'])
        route = output / 'tree' / flow['stages'][-1]['directory'] / 'route.fasm'
        build.convert(output / 'bitstream', route)
        report.update(status='matched', bitstream_sha256=build.digest(output / 'bitstream/model.bit'))
    except Exception as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        receipt.write_text(json.dumps(report, indent=2) + '\n')
