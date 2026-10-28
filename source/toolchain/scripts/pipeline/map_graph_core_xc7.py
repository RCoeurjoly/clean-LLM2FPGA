#!/usr/bin/env python3
"""Map an authenticated RTLIL checkpoint through explicit native XC7 passes.

The RAM phase stops before flip-flop/logic mapping. The final phase maps the
remaining logic, flattens, checks connections and rejects combinational SCCs.
Neither phase establishes numerical correctness, timing or board acceptance.
No generated source is edited by this driver.
"""
import argparse
import gzip
import json
from pathlib import Path

from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt
from compiler.yosys_pipeline import top_flatten_passes


def stage_initializer_images(dependencies, output):
    """Make declared local readmemh images available without rewriting RTL.

    The compiler authenticates local .hex initializers. Yosys resolves their
    names against its working directory, so authenticate the staged copies as
    inputs too. Ambiguous basenames cannot silently choose a different image.
    """
    images = {}
    for name, identity in list(dependencies.items()):
        source = Path(name)
        if source.suffix != '.hex':
            continue
        if source.name in images and images[source.name][1] != identity:
            raise ValueError('conflicting initializer basename: '+source.name)
        images[source.name] = (source, identity)
    output.mkdir(parents=True, exist_ok=True)
    for filename, (source, identity) in images.items():
        target = output/filename
        if target.is_symlink():
            raise ValueError('initializer destination must not be a symlink')
        if source != target:
            target.write_bytes(source.read_bytes())
        dependencies[str(target)] = identity


def validate_serialized_outputs(root: Path, names):
    """Reject empty or corrupt payloads, independently of process exit status.

    Stream gzip data through EOF to check its checksum without materializing
    multi-gigabyte netlists. Semantic native re-read is a separate boundary.
    """
    for name in names:
        path = root / name
        opener = gzip.open if path.suffix == '.gz' else open
        count = 0
        try:
            with opener(path, 'rb') as stream:
                for block in iter(lambda: stream.read(1048576), b''):
                    count += len(block)
        except (OSError, EOFError) as error:
            raise ValueError(f'invalid serialized output: {name}') from error
        if count == 0:
            raise ValueError(f'empty serialized output: {name}')


def qualify_checkpoint(checkpoint: Path, yosys: Path, output: Path,
                       dependencies, tool_hashes, *, final: bool,
                       timeout_seconds=3600):
    """Native re-read must recover main; final netlists also pass check/SCC.

    The separate receipt authenticates the serialized input and native tool.
    It establishes structural qualification only, never numerical equivalence.
    """
    if any(c in str(checkpoint) for c in ('"', '\\', '\n', '\r')):
        raise ValueError('unsupported checkpoint filename')
    passes = [f'read_rtlil "{checkpoint}"',
              'hierarchy -check -top main', 'select *']
    if final:
        passes.extend(('check -assert', 'scc -expect 0'))
    passes.append('tee -o utilization.json stat -json')
    return run_stage(StageSpec('graph-core-serialized-checkpoint',
        (str(yosys), '-p', '; '.join(passes)), dependencies, tool_hashes,
        {'native_passes': json.dumps(passes),
         'scope': 'serialized native checkpoint structural qualification'},
        ('utilization.json',), timeout_seconds), output)


def verify_mapped_checkpoint(directory: Path):
    """Authenticate both mapping and its serialized structural qualification."""
    directory = Path(directory).resolve()
    dependencies = verify_stage_receipt(directory)
    spec = json.loads((directory / 'spec.json').read_text())
    if spec['name'] not in ('graph-core-xc7-coarse', 'graph-core-xc7-ram', 'graph-core-xc7-final'):
        raise ValueError('not a mapped checkpoint stage')
    qualified = directory / 'serialized-checkpoint'
    qualification_dependencies = verify_stage_receipt(qualified)
    qualification_spec = json.loads((qualified / 'spec.json').read_text())
    checkpoint_names = [name for name in spec['outputs']
                        if name in ('checkpoint.il', 'checkpoint.il.gz')]
    if len(checkpoint_names) != 1:
        raise ValueError('mapping must declare one native checkpoint')
    required_passes = [f'read_rtlil "{directory / checkpoint_names[0]}"',
                       'hierarchy -check -top main', 'select *']
    if spec['name'] == 'graph-core-xc7-final':
        required_passes.extend(('check -assert', 'scc -expect 0'))
    required_passes.append('tee -o utilization.json stat -json')
    required_argv = [spec['argv'][0], '-p', '; '.join(required_passes)]
    if qualification_spec['argv'] != required_argv:
        raise ValueError('serialized qualification does not implement mapping policy')
    if (qualification_spec['name'] != 'graph-core-serialized-checkpoint'
            or qualification_spec['tool_hashes'] != spec['tool_hashes']
            or any(qualification_spec['input_hashes'].get(name) != identity
                   for name, identity in dependencies.items())):
        raise ValueError('serialized qualification is not bound to mapping')
    return {**dependencies, **qualification_dependencies}


def map_checkpoint(input_stage: Path, checkpoint: str, yosys: Path,
                   output: Path, *, phase: str, timeout_seconds=3600,
                   compress_outputs=False):
    input_stage, output = Path(input_stage).resolve(), Path(output).resolve()
    if output == input_stage or output.is_relative_to(input_stage):
        raise ValueError('mapping output must be separate from its input stage')
    # Invalidate acceptance before authentication, including failed retries.
    (output/'success.json').unlink(missing_ok=True)
    phases = {'ram': 'map_memory:map_ffram', 'final': 'map_ffram:'}
    if phase not in phases:
        raise ValueError('mapping phase must be ram or final')
    if type(compress_outputs) is not bool:
        raise ValueError('output compression policy must be boolean')
    dependencies = verify_stage_receipt(input_stage)
    input_spec = json.loads((input_stage / 'spec.json').read_text())
    if input_spec['name'] in ('graph-core-xc7-coarse', 'graph-core-xc7-ram', 'graph-core-xc7-final'):
        dependencies = verify_mapped_checkpoint(input_stage)
    receipt = json.loads((input_stage/'success.json').read_text())
    if checkpoint not in receipt['output_hashes']:
        raise ValueError('checkpoint must be an authenticated stage output')
    source = input_stage/checkpoint
    # Yosys accepts quoted filenames; refuse characters with command meaning.
    if any(character in str(source) for character in ('"', '\\', '\n', '\r')):
        raise ValueError('unsupported checkpoint filename')
    yosys = Path(yosys).resolve()
    # The pinned native distribution supplies ABC and mapping definitions as
    # separate files. The Yosys executable's hash cannot authenticate them.
    abc = yosys.with_name('yosys-abc')
    data = yosys.parent.parent / 'share/yosys'
    if not abc.is_file() or data.is_symlink() or not data.is_dir():
        raise ValueError('native Yosys ABC and data directory are required')
    entries = sorted(data.rglob('*'))
    libraries = [path for path in entries if path.is_file()]
    if not libraries or any(path.is_symlink() for path in entries):
        raise ValueError('invalid native Yosys mapping data')
    tool_hashes = {str(path): digest(path) for path in (yosys, abc, *libraries)}
    driver = Path(__file__).resolve()
    dependencies[str(driver)] = digest(driver)
    dependencies[str(Path(run_stage.__code__.co_filename).resolve())] = digest(
        Path(run_stage.__code__.co_filename).resolve())
    passes = [f'read_rtlil "{source}"']
    if phase == 'final':
        policy = Path(top_flatten_passes.__code__.co_filename).resolve()
        dependencies[str(policy)] = digest(policy)
        # Specialize primitive inputs before technology mapping. Mapping each
        # generic primitive first preserves barrel shifters for constant shifts.
        passes.extend((*top_flatten_passes('main'), 'opt'))
    passes.append('synth_xilinx -family xc7 -top main -noiopad -noclkbuf '
                  f'-run {phases[phase]}')
    if phase == 'final':
        passes.extend((*top_flatten_passes('main'), 'check -assert', 'scc -expect 0'))
    suffix = '.gz' if compress_outputs else ''
    checkpoint_output = 'checkpoint.il' + suffix
    passes.extend(('tee -o utilization.json stat -json',
                   'write_rtlil ' + checkpoint_output))
    outputs = ('utilization.json', checkpoint_output)
    if phase == 'final':
        mapped_output = 'mapped.json' + suffix
        passes.append('write_json ' + mapped_output)
        outputs += (mapped_output,)
    result = run_stage(StageSpec('graph-core-xc7-'+phase,
        (str(yosys), '-p', '; '.join(passes)), dependencies,
        tool_hashes,
        {'native_passes': json.dumps(passes),
         'scope': 'XC7 '+phase+' mapping only; DDR/board glue is included only '
                  'if present in the input. No numerical, timing or board acceptance.'},
        outputs, timeout_seconds), output)
    try:
        validate_serialized_outputs(output, outputs)
        serialized_dependencies = verify_stage_receipt(output)
        qualify_checkpoint(output / checkpoint_output, yosys,
                           output / 'serialized-checkpoint',
                           serialized_dependencies, tool_hashes,
                           final=(phase == 'final'),
                           timeout_seconds=timeout_seconds)
    except Exception:
        (output / 'success.json').unlink(missing_ok=True)
        raise
    verify_mapped_checkpoint(output)
    return result


def coarse_checkpoint(input_stage: Path, rtl: str, yosys: Path, output: Path,
                      *, timeout_seconds=3600):
    """Native RTL-to-checkpoint stage, including independent serialized readback.

    Coarse utilization is not final mapped utilization or a fit result.
    """
    input_stage, output = Path(input_stage).resolve(), Path(output).resolve()
    if output == input_stage or output.is_relative_to(input_stage):
        raise ValueError('mapping output must be separate from its input stage')
    (output/'success.json').unlink(missing_ok=True)
    dependencies = verify_stage_receipt(input_stage)
    receipt = json.loads((input_stage/'success.json').read_text())
    if rtl not in receipt['output_hashes']:
        raise ValueError('RTL must be an authenticated stage output')
    source = input_stage/rtl
    if any(c in str(source) for c in ('"', '\\', '\n', '\r')):
        raise ValueError('unsupported RTL filename')
    yosys = Path(yosys).resolve(strict=True)
    abc = yosys.with_name('yosys-abc')
    data = yosys.parent.parent/'share/yosys'
    if not abc.is_file() or data.is_symlink() or not data.is_dir():
        raise ValueError('native Yosys ABC and data directory are required')
    entries = sorted(data.rglob('*'))
    libraries = [p for p in entries if p.is_file()]
    if not libraries or any(p.is_symlink() for p in entries):
        raise ValueError('invalid native Yosys mapping data')
    tools = {str(p): digest(p) for p in (yosys, abc, *libraries)}
    for p in (Path(__file__).resolve(), Path(run_stage.__code__.co_filename).resolve()):
        dependencies[str(p)] = digest(p)
    from compiler.yosys_pipeline import coarse_preparation_passes
    policy = Path(coarse_preparation_passes.__code__.co_filename).resolve()
    dependencies[str(policy)] = digest(policy)
    stage_initializer_images(dependencies, output)
    passes = [f'read_verilog -sv "{source}"',
              *coarse_preparation_passes('main'),
              'tee -o utilization.json stat -json', 'write_rtlil checkpoint.il.gz']
    outputs = ('utilization.json', 'checkpoint.il.gz')
    result = run_stage(StageSpec('graph-core-xc7-coarse',
        (str(yosys), '-p', '; '.join(passes)), dependencies, tools,
        {'native_passes': json.dumps(passes),
         'scope': 'Coarse native mapping only; no final resources, fit, numerical or board acceptance'},
        outputs, timeout_seconds), output)
    try:
        validate_serialized_outputs(output, outputs)
        qualify_checkpoint(output/'checkpoint.il.gz', yosys, output/'serialized-checkpoint',
                           verify_stage_receipt(output), tools, final=False,
                           timeout_seconds=timeout_seconds)
    except Exception:
        (output/'success.json').unlink(missing_ok=True)
        raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input-stage', 'yosys', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--phase', choices=('ram', 'final'), required=True)
    parser.add_argument('--timeout-seconds', type=float, default=3600)
    parser.add_argument('--compress-outputs', action='store_true',
                        help='Use native gzip serialization for large netlists')
    args = parser.parse_args(argv)
    print(map_checkpoint(args.input_stage, args.checkpoint, args.yosys,
        args.output, phase=args.phase,
        timeout_seconds=args.timeout_seconds,
        compress_outputs=args.compress_outputs).identity_sha256)


if __name__ == '__main__':
    main()
