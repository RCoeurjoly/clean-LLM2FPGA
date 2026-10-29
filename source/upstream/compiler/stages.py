"""Run native compiler stages with in-process dependency and output validation."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


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
    name: str
    spec_sha256: str
    output_hashes: dict[str, str]


class StageError(RuntimeError):
    """A native stage failed or did not produce its declared artifacts."""


# Persisted directories cannot substitute for successful execution in this build.
_executions: dict[Path, tuple[StageSpec, StageReceipt]] = {}


def invalidate_stage(directory: Path) -> None:
    _executions.pop(Path(directory).resolve(), None)


def verified_stage(directory: Path) -> tuple[StageSpec, StageReceipt]:
    directory = Path(directory).resolve()
    try:
        spec, receipt = _executions[directory]
    except KeyError as error:
        raise ValueError(f'stage did not complete in this build: {directory}') from error
    if _identity(asdict(spec)) != receipt.spec_sha256:
        raise ValueError('stage specification changed after execution')
    _check_identities(spec)
    for name, identity in receipt.output_hashes.items():
        path = directory / name
        if (path.is_symlink() or not path.resolve().is_relative_to(directory)
                or not path.is_file() or digest(path) != identity):
            raise ValueError(f'stage output changed or missing: {path}')
    return spec, receipt


def verify_stage_receipt(directory: Path) -> dict[str, str]:
    """Validate this process's completed execution and return its dependencies."""
    directory = Path(directory).resolve()
    spec, receipt = verified_stage(directory)
    return {**spec.input_hashes, **spec.tool_hashes,
            **{str(directory / name): identity
               for name, identity in receipt.output_hashes.items()}}


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
    output_dir = Path(output_dir).resolve()
    invalidate_stage(output_dir)
    if not spec.name or not spec.argv or spec.argv[0] not in spec.tool_hashes:
        raise ValueError('stage must declare its executable identity')
    if any(not Path(name).is_absolute()
           for name in (*spec.input_hashes, *spec.tool_hashes)):
        raise ValueError('input and tool identities must use absolute paths')
    if spec.timeout_seconds <= 0 or len(set(spec.outputs)) != len(spec.outputs) or not spec.outputs:
        raise ValueError('stage outputs and limits are invalid')
    for name in spec.outputs:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError(f'invalid output path: {name}')
        if not (output_dir / name).resolve().is_relative_to(output_dir.resolve()):
            raise ValueError(f'output path escapes stage: {name}')
    output_dir.mkdir(parents=True, exist_ok=True)
    # Invalidate previous acceptance before checking inputs or starting a tool.
    for name in spec.outputs:
        path = output_dir / name
        if path.exists() or path.is_symlink():
            if path.is_dir():
                raise ValueError(f'output path is a directory: {name}')
            path.unlink()
    spec_hash = _identity(asdict(spec))
    hashes = {}
    try:
        print(f'[{spec.name}] {" ".join(spec.argv)}', file=sys.stderr, flush=True)
        _check_identities(spec)
        subprocess.run(spec.argv, cwd=output_dir,
                       env={**os.environ, **spec.environment},
                       timeout=spec.timeout_seconds, check=True)
        _check_identities(spec)
        for name in spec.outputs:
            path = output_dir / name
            if not path.is_file() or path.is_symlink():
                raise ValueError(f'missing declared output: {name}')
            if not path.resolve().is_relative_to(output_dir):
                raise ValueError(f'output path escapes stage: {name}')
            hashes[name] = digest(path)
    except subprocess.TimeoutExpired as error:
        raise StageError(f'{spec.name}: compiler exceeded stage timeout') from error
    except subprocess.CalledProcessError as error:
        raise StageError(f'{spec.name}: compiler exited with {error.returncode}') from error
    except (OSError, ValueError) as error:
        raise StageError(f'{spec.name}: {error}') from error
    receipt = StageReceipt(spec.name, spec_hash, hashes)
    _executions[output_dir] = (spec, receipt)
    return receipt
