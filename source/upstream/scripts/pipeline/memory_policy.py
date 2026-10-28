"""Memory choices and image bindings are compiler inputs, never SV edits."""
from __future__ import annotations
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

@dataclass(frozen=True)
class MemorySpec:
    name: str
    width: int
    depth: int
    implementation: str
    read_latency: int
    writable: bool
    init_file: str | None

# Preserve the measured c_fc/c_proj placement distinction. This list describes
# logical compiler memories; its consumers must resolve aliases before lookup.
LUTRAM_NAMES = frozenset({
    'attention_context_q16_16', 'attention_residual_q16_16',
    'block_input_q16_16', 'block_output_q16_16', 'ln1_output_q16_16',
    'ln2_output_q16_16', 'final_output_q16_16', 'q_input_codes_i8',
    'q_input_q16_16', 'q_accumulator_i64', 'q_output_q16_16', 'k_input_q16_16',
    'k_output_q16_16', 'v_input_q16_16', 'v_output_q16_16', 'out_input_q16_16',
    'out_output_q16_16', 'c_fc_input_codes_i8', 'c_fc_input_q16_16',
    'c_proj_input_q16_16', 'c_proj_output_q16_16', 'attention_score_q8',
    'attention_delta_q8', 'attention_exp_q1_20', 'c_fc_output_codes_i8',
    'c_fc_output_q16_16', 'c_proj_post_weight_rescale_bias_q16_16',
    'lm_input_q16_16', 'token_weight_scale_q8_24_b0',
    'token_weight_scale_q8_24_b1', 'token_weight_scale_q8_24_b2',
    'token_weight_scale_q8_24_b3',
})

def select_memory(spec: MemorySpec, profile: str) -> MemorySpec:
    if profile not in ('one-rom', 'two-rom'):
        raise ValueError('unknown memory profile')
    if spec.width < 1 or spec.depth < 1:
        raise ValueError('invalid memory geometry')
    if spec.depth < 512 or spec.width > 72:
        return replace(spec, implementation='register', read_latency=1)
    lutram = spec.name in LUTRAM_NAMES or (profile == 'one-rom' and spec.name == 'c_proj_weight_codes_i8')
    return replace(spec, implementation='lutram' if lutram else 'bram', read_latency=2)


def validate_image(spec: MemorySpec, directory: Path) -> dict:
    if spec.init_file is None or Path(spec.init_file).name != spec.init_file:
        raise ValueError(f'{spec.name}: missing or unsafe image binding')
    try:
        data = (directory / spec.init_file).read_bytes()
        rows = data.decode('ascii').splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f'{spec.name}: missing or invalid image') from error
    digits = (spec.width + 3) // 4
    if len(rows) != spec.depth or any(len(row) != digits or not re.fullmatch('[0-9a-fA-F]+', row)
                                     or int(row, 16) >= 1 << spec.width for row in rows):
        raise ValueError(f'{spec.name}: image width/depth mismatch')
    return {'width': spec.width, 'elements': spec.depth, 'sha256': hashlib.sha256(data).hexdigest()}


def emit_memory_sources(specs: list[MemorySpec], *, include_narrow_rom=False) -> dict:
    """Emit owned sources; native IR binding declares optional narrow ROMs.

    The returned legacy Futil declarations and logical bindings keep their
    original ABI. Narrow-ROM extern declarations come from the native pass.
    """
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError('duplicate memory binding')
    images = sorted(s.init_file for s in specs if s.init_file is not None)
    if len(images) != len(set(images)):
        raise ValueError('duplicate initialization image')
    for spec in specs:
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', spec.name):
            raise ValueError('unsafe memory name')
        if spec.width < 1 or spec.depth < 1 or spec.implementation not in ('register', 'bram', 'lutram'):
            raise ValueError('invalid memory specification')
        if not spec.writable and spec.init_file is None:
            raise ValueError(f'{spec.name}: immutable memory requires an image')
        if spec.init_file is not None and not re.fullmatch('[A-Za-z0-9_-]+[.]hex', spec.init_file):
            raise ValueError('unsafe image filename')
        if spec.read_latency != (1 if spec.implementation == 'register' else 2):
            raise ValueError('memory primitive/latency mismatch')
    ids = {name: index + 1 for index, name in enumerate(images)}
    bindings = {spec.name: {'primitive': 'closure_' + spec.implementation + '_memory',
                           'parameters': [spec.width, spec.depth, max(1, (spec.depth - 1).bit_length()),
                                          ids.get(spec.init_file, 0), int(spec.writable)],
                           'init_file': spec.init_file, 'read_latency': spec.read_latency}
                for spec in specs}
    template = Path(__file__).resolve().parents[2] / 'rtl/primitives/closure_memory.sv'
    from string import Template
    initializer = '\n'.join(f'      {number}: $readmemh("{name}", mem);' for name, number in ids.items())
    sv = '\n'.join(Template(template.read_text()).substitute(
        module='closure_' + kind + '_memory', style=style, latency=latency, initializers=initializer)
        for kind, style, latency in (('register', 'auto', 1), ('bram', 'block', 2), ('lutram', 'distributed', 2)))
    declaration = (Path(__file__).resolve().parents[2] / 'calyx/primitives/closure_memory.futil').read_text()
    if include_narrow_rom:
        narrow_template = template.with_name('closure_narrow_rom.sv').read_text()
        sv += '\n' + '\n'.join(Template(narrow_template).substitute(
            module='closure_' + kind + '_memory_narrow', style=style,
            latency=latency, initializers=initializer)
            for kind, style, latency in (('register', 'auto', 1),
                                        ('bram', 'block', 2), ('lutram', 'distributed', 2)))
    return {'sv': sv, 'futil': declaration, 'bindings': bindings, 'images': ids}
