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
    calyx_library: Path
    contexts: tuple[int, ...] = tuple(range(4, 20))
    memory_implementation: str = 'bram'
    narrow_immutable: bool = True
    offchip_immutable: bool = False
    recover_restoring_sqrt_loops: bool = False
    coalesce_pointwise_pingpong: bool = False
    fuse_gemv_epilogues: bool = False
    pool_private_capacity: bool = False
    stable_scratch_pooling: bool = False
    lut_memory_max_bits: int = 0
    _document: dict = field(init=False, repr=False)
    identity_sha256: str = field(init=False)

    def __post_init__(self):
        if (type(self.contexts) is not tuple or self.contexts != tuple(range(4, 20))
                or any(type(length) is not int for length in self.contexts)):
            raise ValueError('generation requires ordered unique contexts 4 through 19')
        if set(self.tools) != {'torch', 'circt', 'memory', 'translate', 'calyx'}:
            raise ValueError('invalid compiler tool roles')
        if (self.memory_implementation not in ('register', 'bram', 'lutram')
                or type(self.narrow_immutable) is not bool
                or type(self.offchip_immutable) is not bool):
            raise ValueError('invalid memory policy')
        if type(self.fuse_gemv_epilogues) is not bool or type(self.pool_private_capacity) is not bool:
            raise ValueError('memory reduction policies must be boolean')
        if type(self.lut_memory_max_bits) is not int or self.lut_memory_max_bits < 0:
            raise ValueError('LUT memory threshold must be a nonnegative integer')
        if self.lut_memory_max_bits and self.memory_implementation != 'bram':
            raise ValueError('LUT memory threshold requires a BRAM base implementation')
        if type(self.stable_scratch_pooling) is not bool:
            raise ValueError('stable scratch pooling policy must be boolean')
        if self.stable_scratch_pooling and not self.pool_private_capacity:
            raise ValueError('stable scratch pooling requires capacity pooling')
        if type(self.coalesce_pointwise_pingpong) is not bool:
            raise ValueError('pointwise coalescing policy must be boolean')
        if type(self.recover_restoring_sqrt_loops) is not bool:
            raise ValueError('sqrt recovery policy must be boolean')
        if self.offchip_immutable and self.narrow_immutable:
            raise ValueError('off-chip storage narrowing is not qualified')
        object.__setattr__(self, 'numerical_contract', Path(self.numerical_contract).resolve(strict=True))
        object.__setattr__(self, 'calyx_library', Path(self.calyx_library).resolve(strict=True))
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
                    'compiler/graph_images.py', 'compiler/memory_plan.py', 'compiler/ddr_images.py',
                    'scripts/pipeline/memory_policy.py', 'compiler/build_profile.py',
                    'compiler/build_core.py', 'compiler/compile_core.py',
                    'compiler/ddr_transport.py', 'compiler/ddr_bank_adapter.py', 'compiler/clock_contract.py',
                    'rtl/primitives/closure_ddr_read_port.sv', 'rtl/primitives/closure_ddr_read_bank.sv',
                    'rtl/primitives/closure_ddr_read_cdc.sv', 'rtl/primitives/closure_ddr_image_loader.sv',
                    'rtl/primitives/closure_ddr_loaded_port.sv',
                    'rtl/primitives/closure_memory.sv',
                    'rtl/primitives/closure_narrow_rom.sv',
                    'calyx/primitives/closure_memory.futil']
        sources += ['nix/graph-core-source.nix', 'nix/graph-core-data.nix', 'nix/graph-core.nix']
        paths = [manifest, self.numerical_contract, *(root / name for name in sources)]
        library = sorted(path for path in self.calyx_library.rglob('*') if path.is_file())
        if not self.calyx_library.is_dir() or not library:
            raise ValueError('Calyx library must contain primitive sources')
        paths.extend(library)
        dependencies = {str(path): digest(path) for path in paths}
        tool_hashes = {name: tool.identities() for name, tool in sorted(self.tools.items())}
        return {'schema_version': 1, 'profile': 'graph-generation-core-v1',
                'contexts': list(self.contexts),
                'numerical_contract_sha256': digest(self.numerical_contract),
                'memory_policy': {'implementation': self.memory_implementation,
                                  'narrow_immutable': self.narrow_immutable,
                                  'offchip_immutable': self.offchip_immutable,
                                  'lut_memory_max_bits': self.lut_memory_max_bits,
                                  'ddr_layout': {'data_width': 576, 'address_width': 25}
                                                if self.offchip_immutable else None},
                'optimization_policy': {
                    'recover_restoring_sqrt_loops': self.recover_restoring_sqrt_loops,
                    'coalesce_pointwise_pingpong': self.coalesce_pointwise_pingpong,
                    'fuse_gemv_epilogues': self.fuse_gemv_epilogues,
                    'pool_private_capacity': self.pool_private_capacity,
                    'stable_scratch_pooling': self.stable_scratch_pooling},
                'runtime_interface': {'roles': ['prompt', 'generated_tokens', 'last_logits'],
                                      'binding_pass': '--llm2fpga-expose-runtime-memories',
                                      'calyx_pass_alias': 'external'},
                'pass_sequence': [{'name': name, 'tool': tool, 'arguments': list(arguments),
                                   'output': filename}
                                  for name, tool, arguments, filename in integer_profile(entrypoint='main', bytecode=True, runtime_roles=True,
                                      recover_restoring_sqrt_loops=self.recover_restoring_sqrt_loops,
                                      coalesce_pointwise_pingpong=self.coalesce_pointwise_pingpong,
                                      fuse_gemv_epilogues=self.fuse_gemv_epilogues,
                                      pool_private_capacity=self.pool_private_capacity,
                                      stable_scratch_pooling=self.stable_scratch_pooling)],
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
