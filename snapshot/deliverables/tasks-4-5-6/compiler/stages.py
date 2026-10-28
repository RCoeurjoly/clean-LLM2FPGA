"""Execute declared compiler stages and retain honest, content-addressed receipts.

This module runs native tools. It never parses or rewrites emitted computation.
Measured duration is separate from the deterministic compilation receipt.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time


def digest(path: Path) -> str:
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _identity(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class StageSpec:
    name: str
    argv: tuple[str, ...]
    input_hashes: dict[str, str]
    tool_hashes: dict[str, str]
    pass_identities: dict[str, str]
    outputs: tuple[str, ...]
    timeout_seconds: float = 120
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StageReceipt:
    schema_version: int
    name: str
    success: bool
    spec_sha256: str
    identity_sha256: str
    output_hashes: dict[str, str]
    exit_code: int | None
    signal: int | None
    diagnostic: str


class StageError(RuntimeError):
    def __init__(self, receipt: StageReceipt):
        super().__init__(f'{receipt.name}: {receipt.diagnostic}')
        self.receipt = receipt


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    temporary.replace(path)


def verify_stage_receipt(directory: Path) -> dict[str, str]:
    """Authenticate declared successful stage evidence, returning its dependencies.

    This checks execution and file identities; it does not prove semantics.
    It reads declared metadata and artifacts only, never inferred model answers.
    """
    directory = Path(directory).resolve()
    try:
        spec_path, receipt_path = directory / 'spec.json', directory / 'success.json'
        spec = json.loads(spec_path.read_text())
        receipt = json.loads(receipt_path.read_text())
        if (receipt['schema_version'] != 1 or receipt['success'] is not True
                or receipt['name'] != spec['name'] or receipt['exit_code'] != 0
                or receipt['signal'] is not None or receipt['diagnostic']
                or receipt['spec_sha256'] != _identity(spec)
                or receipt['identity_sha256'] != _identity({
                    'spec_sha256': receipt['spec_sha256'],
                    'output_hashes': receipt['output_hashes']})):
            raise ValueError('unsuccessful or invalid stage receipt')
        outputs = spec['outputs']
        if (not isinstance(outputs, list) or not outputs
                or len(set(outputs)) != len(outputs)
                or set(receipt['output_hashes']) != set(outputs)):
            raise ValueError('stage outputs differ from specification')
        dependencies = {str(spec_path): digest(spec_path), str(receipt_path): digest(receipt_path)}
        for kind in ('input_hashes', 'tool_hashes'):
            identities = spec[kind]
            if not isinstance(identities, dict) or not identities:
                raise ValueError('missing stage dependency identities')
            for name, identity in identities.items():
                path = Path(name)
                if not path.is_absolute() or not path.is_file() or digest(path) != identity:
                    raise ValueError('stage dependency changed or missing')
                dependencies[name] = identity
        if not spec['argv'] or spec['argv'][0] not in spec['tool_hashes']:
            raise ValueError('stage executable identity missing')
        for name, identity in receipt['output_hashes'].items():
            relative = PurePosixPath(name)
            path = directory / relative
            if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                    or not path.resolve().is_relative_to(directory) or path.is_symlink()
                    or not path.is_file() or digest(path) != identity):
                raise ValueError('stage output changed or invalid')
            dependencies[str(path)] = identity
        return dependencies
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f'invalid stage evidence: {error}') from error


def _check_identities(spec):
    for kind, identities in (('input', spec.input_hashes), ('tool', spec.tool_hashes)):
        for name, expected in identities.items():
            try:
                actual = digest(Path(name))
            except OSError as error:
                raise ValueError(f'missing {kind}: {name}') from error
            if actual != expected:
                raise ValueError(f'{kind} identity mismatch: {name}')


def run_stage(spec: StageSpec, output_dir: Path) -> StageReceipt:
    output_dir = Path(output_dir)
    reserved = {'spec.json', 'success.json', 'failure.json', 'observations.json', 'stdout.log', 'stderr.log'}
    if not spec.name or not spec.argv or spec.argv[0] not in spec.tool_hashes:
        raise ValueError('stage must declare its executable identity')
    if any(not Path(name).is_absolute()
           for name in (*spec.input_hashes, *spec.tool_hashes)):
        raise ValueError('input and tool identities must use absolute paths')
    if spec.timeout_seconds <= 0 or len(set(spec.outputs)) != len(spec.outputs) or not spec.outputs:
        raise ValueError('stage outputs and limits are invalid')
    for name in spec.outputs:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or name in reserved or not path.parts:
            raise ValueError(f'invalid output path: {name}')
        if not (output_dir / name).resolve().is_relative_to(output_dir.resolve()):
            raise ValueError(f'output path escapes stage: {name}')
    output_dir.mkdir(parents=True, exist_ok=True)
    # Invalidate previous acceptance before checking inputs or starting a tool.
    for name in reserved | set(spec.outputs):
        path = output_dir / name
        if path.exists() or path.is_symlink():
            if path.is_dir():
                raise ValueError(f'output path is a directory: {name}')
            path.unlink()
    spec_hash = _identity(asdict(spec))
    # The digest alone cannot show reviewers which inputs, tools and passes ran.
    _write_json(output_dir / 'spec.json', asdict(spec))
    started = time.monotonic()
    exit_code, signal, diagnostic = None, None, ''
    hashes = {}
    try:
        # Native streams go directly to retained files: long-running compiler
        # progress is observable, and diagnostics do not consume Python RAM or
        # depend on a text encoding. subprocess.run still kills/reaps its child
        # on timeout; partial bytes remain in the same files.
        with (output_dir/'stdout.log').open('wb') as stdout, (output_dir/'stderr.log').open('wb') as stderr:
            _check_identities(spec)
            completed = subprocess.run(spec.argv, cwd=output_dir,
                                       env={**os.environ, **spec.environment},
                                       stdout=stdout, stderr=stderr, timeout=spec.timeout_seconds)
        exit_code = completed.returncode
        signal = -exit_code if exit_code < 0 else None
        if exit_code:
            diagnostic = f'compiler exited with {exit_code}'
        else:
            _check_identities(spec)
            for name in spec.outputs:
                path = output_dir / name
                if not path.is_file() or path.is_symlink():
                    raise ValueError(f'missing declared output: {name}')
                if not path.resolve().is_relative_to(output_dir.resolve()):
                    raise ValueError(f'output path escapes stage: {name}')
                hashes[name] = digest(path)
    except subprocess.TimeoutExpired:
        diagnostic = 'compiler exceeded stage timeout'
    except (OSError, ValueError) as error:
        diagnostic = str(error)
    success = not diagnostic and exit_code == 0
    identity = _identity({'spec_sha256': spec_hash, 'output_hashes': hashes})
    receipt = StageReceipt(1, spec.name, success, spec_hash, identity, hashes,
                           exit_code, signal, diagnostic)
    _write_json(output_dir / 'observations.json', {'elapsed_seconds': time.monotonic() - started})
    _write_json(output_dir / ('success.json' if success else 'failure.json'), asdict(receipt))
    if not success:
        raise StageError(receipt)
    return receipt
