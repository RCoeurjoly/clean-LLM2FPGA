"""Authenticate compilation data without opening golden-result artifacts.

This stage owns bytes and metadata only. Tensor conversion belongs to the next
stage; authentication must not silently quantize or reconstruct a model.
"""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CompileInputs:
    config: dict[str, Any]
    manifest: dict[str, Any]
    geometry: dict[str, Any]
    arithmetic: dict[str, Any]
    weights: bytes
    scales: bytes
    input_hashes: dict[str, dict[str, str]]
    contract_sha256: str
    materialized_sha256: str

    def _materialized_identity(self):
        data = {'config': self.config, 'manifest': self.manifest,
                'geometry': self.geometry, 'arithmetic': self.arithmetic,
                'weights_sha256': hashlib.sha256(self.weights).hexdigest(),
                'scales_sha256': hashlib.sha256(self.scales).hexdigest(),
                'input_hashes': self.input_hashes, 'contract_sha256': self.contract_sha256}
        return hashlib.sha256(json.dumps(data, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def validate(self):
        """Reject in-memory changes since the authenticated loader returned."""
        if self._materialized_identity() != self.materialized_sha256:
            raise ValueError('materialized compiler inputs changed')
        for name, data in (('weights.bin', self.weights), ('scales.bin', self.scales)):
            if hashlib.sha256(data).hexdigest() != self.input_hashes['package'][name]:
                raise ValueError('package payload identity mismatch: ' + name)


_INPUT_FILES = {
    'snapshot': frozenset(('config.json', 'pytorch_model.bin')),
    'package': frozenset(('manifest.json', 'weights.bin', 'scales.bin')),
}


def _object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'invalid {label}: {error}') from error
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    return value


def load_compile_inputs(snapshot: Path, package: Path,
                        numerical_contract: Path) -> CompileInputs:
    contract_bytes = Path(numerical_contract).read_bytes()
    contract = _object(contract_bytes, 'numerical contract')
    if contract.get('schema_version') != 1:
        raise ValueError('unsupported numerical contract schema')
    identities = contract.get('inputs')
    if not isinstance(identities, dict) or set(identities) != set(_INPUT_FILES):
        raise ValueError('compiler input allowlist differs')
    # Validate the entire allowlist before opening any named file. A contract
    # cannot smuggle an expected-answer file into the compiler dependency set.
    for kind, names in _INPUT_FILES.items():
        if not isinstance(identities[kind], dict) or set(identities[kind]) != names:
            raise ValueError(f'{kind} input allowlist differs')
    payloads = {}
    hashes = {}
    for kind, directory in (('snapshot', Path(snapshot)), ('package', Path(package))):
        payloads[kind] = {}
        hashes[kind] = {}
        for name in sorted(_INPUT_FILES[kind]):
            path = directory / name
            try:
                data = path.read_bytes()
            except OSError as error:
                raise ValueError(f'missing compiler input: {kind}/{name}') from error
            digest = hashlib.sha256(data).hexdigest()
            if digest != identities[kind][name]:
                raise ValueError(f'compiler input identity mismatch: {kind}/{name}')
            hashes[kind][name] = digest
            payloads[kind][name] = data
    config = _object(payloads['snapshot']['config.json'], 'snapshot config')
    manifest = _object(payloads['package']['manifest.json'], 'package manifest')
    geometry = manifest.get('model')
    arithmetic = contract.get('arithmetic')
    if not isinstance(geometry, dict) or not isinstance(arithmetic, dict):
        raise ValueError('model geometry and arithmetic must be objects')
    inputs = CompileInputs(config, manifest, geometry, arithmetic,
                         payloads['package']['weights.bin'], payloads['package']['scales.bin'],
                         hashes, hashlib.sha256(contract_bytes).hexdigest(), '')
    return replace(inputs, materialized_sha256=inputs._materialized_identity())
