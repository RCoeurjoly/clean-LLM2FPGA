"""Authenticate the common graph compiler's declared inputs and pass sequence."""
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .pipeline import CompilerTool, integer_profile
from .stages import digest, _identity


@dataclass(frozen=True)
class CoreBuildProfile:
    numerical_contract: Path
    tools: Mapping[str, CompilerTool]
    contexts: tuple[int, ...] = tuple(range(4, 20))
    recover_restoring_sqrt_loops: bool = False
    coalesce_pointwise_pingpong: bool = False
    fuse_gemv_epilogues: bool = False
    pool_private_capacity: bool = False
    stable_scratch_pooling: bool = False
    _document: dict = field(init=False, repr=False)
    identity_sha256: str = field(init=False)

    def __post_init__(self):
        if (type(self.contexts) is not tuple or self.contexts != tuple(range(4, 20))
                or any(type(length) is not int for length in self.contexts)):
            raise ValueError('generation requires ordered unique contexts 4 through 19')
        if set(self.tools) != {'torch'}:
            raise ValueError('invalid compiler tool roles')
        if type(self.fuse_gemv_epilogues) is not bool or type(self.pool_private_capacity) is not bool:
            raise ValueError('memory reduction policies must be boolean')
        if type(self.stable_scratch_pooling) is not bool:
            raise ValueError('stable scratch pooling policy must be boolean')
        if self.stable_scratch_pooling and not self.pool_private_capacity:
            raise ValueError('stable scratch pooling requires capacity pooling')
        if type(self.coalesce_pointwise_pingpong) is not bool:
            raise ValueError('pointwise coalescing policy must be boolean')
        if type(self.recover_restoring_sqrt_loops) is not bool:
            raise ValueError('sqrt recovery policy must be boolean')
        object.__setattr__(self, 'numerical_contract', Path(self.numerical_contract).resolve(strict=True))
        object.__setattr__(self, 'tools', MappingProxyType(dict(self.tools)))
        document = self._current_document()
        object.__setattr__(self, '_document', document)
        object.__setattr__(self, 'identity_sha256', _identity(document))

    def _current_document(self):
        root = Path(__file__).resolve().parents[1]
        manifest = root / 'compiler/frontend/dependencies.json'
        sources = json.loads(manifest.read_text())['compilation_sources']
        sources += ['compiler/frontend/capture.py', 'compiler/frontend/operator_support.json',
                    'compiler/pipeline.py', 'compiler/stages.py', 'compiler/generation_driver.py',
                    'compiler/graph_images.py', 'compiler/build_profile.py',
                    'compiler/build_core.py', 'compiler/compile_core.py']
        paths = [manifest, self.numerical_contract, *(root / name for name in sources)]
        dependencies = {str(path): digest(path) for path in paths}
        tool_hashes = {name: tool.identities() for name, tool in sorted(self.tools.items())}
        return {'schema_version': 1, 'profile': 'graph-generation-core-v1',
                'contexts': list(self.contexts),
                'numerical_contract_sha256': digest(self.numerical_contract),
                'optimization_policy': {
                    'recover_restoring_sqrt_loops': self.recover_restoring_sqrt_loops,
                    'coalesce_pointwise_pingpong': self.coalesce_pointwise_pingpong,
                    'fuse_gemv_epilogues': self.fuse_gemv_epilogues,
                    'pool_private_capacity': self.pool_private_capacity,
                    'stable_scratch_pooling': self.stable_scratch_pooling},
                'pass_sequence': [{'name': name, 'tool': tool, 'arguments': list(arguments),
                                   'output': filename}
                                  for name, tool, arguments, filename in integer_profile(entrypoint='main',
                                      recover_restoring_sqrt_loops=self.recover_restoring_sqrt_loops,
                                      coalesce_pointwise_pingpong=self.coalesce_pointwise_pingpong,
                                      fuse_gemv_epilogues=self.fuse_gemv_epilogues,
                                      pool_private_capacity=self.pool_private_capacity,
                                      stable_scratch_pooling=self.stable_scratch_pooling)
                                  if name in {'standard', 'buffers', 'scratch', 'loops'}],
                'dependency_hashes': dependencies, 'tool_hashes': tool_hashes}

    def validate(self):
        try:
            current = self._current_document()
        except OSError as error:
            raise ValueError('profile dependency changed or missing') from error
        if _identity(current) != self.identity_sha256:
            raise ValueError('profile dependency changed')

    def document(self):
        self.validate()
        return deepcopy(self._document)
