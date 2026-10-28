#!/usr/bin/env python3
"""Compose the authenticated TinyStories model from existing compiler phases.

This emits one hardware controller and one shared transformer datapath.  Eight
row slots are allocated; rows beyond the real 4/5-token context are causally
irrelevant padding.  Only scheduling/address capacities are widened.  Existing
fixed-point arithmetic generators are not modified.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from scripts.pipeline.tinystories_family_memory_plan import (
    build_token_code_plan, materialize_token_code_bank,
)
from scripts.pipeline.tinystories_family_spec import FamilyModelSpec

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "tinystories-1m-model-orchestrator-calyx-v1"
ORACLE = ROOT / "artifacts/reference/tinystories-1m-fixed-point-model-token-step-oracle.json"
Phase = tuple[list[str], list[str], str]

# The Yosys XC7 BRAM mapper has a bounded address depth.  Keep the exact
# token-embedding tensor order, but bank it by 256-token rows so every
# production memory is depth-16384.  This is the largest depth accepted by the
# installed XC7 BRAM library mapper for the generated synchronous memories.
# The final bank is padded with zeros; all valid TinyStories vocabulary
# addresses remain unchanged.
TOKEN_CODE_BANKS = 197
TOKEN_CODE_BANK_DEPTH = 16384
TOKEN_CODE_ROW_BITS = 8
# Packed production layout: eight signed INT8 codes share one 64-bit BRAM
# word.  A depth-16384 bank therefore stores 2048 vocabulary rows, reducing
# the physical bank count from 197 to 25 while preserving row-major order.
PACKED_TOKEN_CODE_LANES = 8
_DEFAULT_TOKEN_CODE_PLAN = build_token_code_plan(vocab=50257, hidden=64,
                                                  bank_depth=TOKEN_CODE_BANK_DEPTH)
PACKED_TOKEN_CODE_ROWS_PER_BANK = _DEFAULT_TOKEN_CODE_PLAN.rows_per_bank
PACKED_TOKEN_CODE_BANKS = _DEFAULT_TOKEN_CODE_PLAN.bank_count
# Scale rows are 32-bit values. A 2,048-row bank maps to two 36-bit
# RAMB36E1-width slices, avoiding the padding cost of a 16,384-row bank.
TOKEN_SCALE_BANK_DEPTH = 2048
TOKEN_SCALE_BANKS = (50257 + TOKEN_SCALE_BANK_DEPTH - 1) // TOKEN_SCALE_BANK_DEPTH


def packed_token_code_location(token: int, hidden: int, *, model_hidden: int = 64,
                               vocab: int = 50257) -> tuple[int, int, int]:
    """Return (bank, word address, byte lane) for an exact model code."""
    plan = (_DEFAULT_TOKEN_CODE_PLAN if model_hidden == 64 and vocab == 50257
            else build_token_code_plan(vocab=vocab, hidden=model_hidden,
                                       bank_depth=TOKEN_CODE_BANK_DEPTH))
    return plan.location(token, hidden)


def pack_token_code_words(codes: list[int]) -> list[int]:
    """Pack row-major signed INT8 token codes into little-endian 64-bit words."""
    if len(codes) == 0 or len(codes) % 64:
        raise ValueError("token code tensor must contain complete 64-value rows")
    return pack_int8_words(codes, PACKED_TOKEN_CODE_LANES)


def pack_int8_words(codes: list[int], lanes: int) -> list[int]:
    """Pack signed INT8 values into little-endian byte-lane words.

    ``lanes`` is deliberately explicit so projection memories can use four
    byte lanes while token embeddings retain their existing eight-lane ABI.
    The operation is a storage representation only: every input byte is
    preserved verbatim in two's-complement form.
    """
    if type(lanes) is not int or lanes <= 0 or lanes > 8 or lanes & (lanes - 1):
        raise ValueError("lanes must be a power of two in the range 1..8")
    if len(codes) == 0 or len(codes) % lanes:
        raise ValueError("INT8 values must contain complete packed words")
    if any(type(code) is not int or not -128 <= code <= 127 for code in codes):
        raise ValueError("INT8 values contain a non-INT8 value")
    words = []
    for offset in range(0, len(codes), lanes):
        word = 0
        for lane, code in enumerate(codes[offset : offset + lanes]):
            word |= (code & 0xFF) << (8 * lane)
        words.append(word)
    return words


def unpack_int8_words(words: list[int], lanes: int) -> list[int]:
    """Inverse of :func:`pack_int8_words`, returning signed INT8 values."""
    if type(lanes) is not int or lanes <= 0 or lanes > 8 or lanes & (lanes - 1):
        raise ValueError("lanes must be a power of two in the range 1..8")
    values = []
    for word in words:
        if type(word) is not int or not 0 <= word < (1 << (8 * lanes)):
            raise ValueError("packed word is outside its lane width")
        for lane in range(lanes):
            byte = (word >> (8 * lane)) & 0xFF
            values.append(byte - 256 if byte & 0x80 else byte)
    return values


def pack_sparse_int8_rows(codes: list[int], inputs: int) -> tuple[list[int], list[int], list[int]]:
    """Encode row-major INT8 codes as (bitmaps, offsets, values).

    Each row has one bitmap bit per input index and a compact list of its
    nonzero signed INT8 values in ascending input order.  Offsets has one
    entry per row plus a terminal entry, so decoding is random-access by row
    without relying on variable-width parsing.  Zero rows are represented by
    an all-zero bitmap and equal consecutive offsets.
    """
    if type(inputs) is not int or inputs <= 0:
        raise ValueError("inputs must be a positive integer")
    if len(codes) == 0 or len(codes) % inputs:
        raise ValueError("sparse tensor must contain complete rows")
    if any(type(code) is not int or not -128 <= code <= 127 for code in codes):
        raise ValueError("sparse tensor contains a non-INT8 value")
    rows = len(codes) // inputs
    bitmap_words = (inputs + 31) // 32
    bitmaps: list[int] = []
    offsets = [0]
    values: list[int] = []
    for row in range(rows):
        bitmap = [0] * bitmap_words
        for index, code in enumerate(codes[row * inputs : (row + 1) * inputs]):
            if code:
                bitmap[index // 32] |= 1 << (index % 32)
                values.append(code)
        bitmaps.extend(bitmap)
        offsets.append(len(values))
    return bitmaps, offsets, values


def unpack_sparse_int8_rows(bitmaps: list[int], offsets: list[int], values: list[int], inputs: int) -> list[int]:
    """Decode :func:`pack_sparse_int8_rows`, validating its structural ABI."""
    if type(inputs) is not int or inputs <= 0:
        raise ValueError("inputs must be a positive integer")
    bitmap_words = (inputs + 31) // 32
    if len(offsets) < 1 or (len(offsets) - 1) * bitmap_words != len(bitmaps):
        raise ValueError("sparse bitmap/offset dimensions disagree")
    if offsets[0] != 0 or any(type(x) is not int or x < 0 for x in offsets):
        raise ValueError("sparse offsets must be nonnegative and start at zero")
    if any(offsets[i] > offsets[i + 1] for i in range(len(offsets) - 1)) or offsets[-1] != len(values):
        raise ValueError("sparse offsets are not monotonic or do not end at values")
    if any(type(x) is not int or not -128 <= x <= 127 for x in values):
        raise ValueError("sparse values contain a non-INT8 value")
    result: list[int] = []
    for row in range(len(offsets) - 1):
        row_bitmap = bitmaps[row * bitmap_words : (row + 1) * bitmap_words]
        cursor = offsets[row]
        end = offsets[row + 1]
        for index in range(inputs):
            present = (row_bitmap[index // 32] >> (index % 32)) & 1
            if present:
                if cursor >= end:
                    raise ValueError("sparse bitmap has too many nonzero entries")
                result.append(values[cursor])
                cursor += 1
            else:
                result.append(0)
        if cursor != end:
            raise ValueError("sparse bitmap has too few nonzero entries")
    return result


@dataclass(frozen=True)
class CalyxArtifact:
    futil: str
    provenance: dict[str, Any]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _dependencies():
    block = _load(ROOT / "scripts/pipeline/lower_fixed_point_block_composition_to_calyx.py", "model_block_lowerer")
    return block, block._attention_lowerer(), block._mlp_lowerer()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _phase_program(phase: Phase):
    from scripts.pipeline.calyx_program import Program
    cells, wires, control = phase
    return Program.parse("component main() -> () { cells {" + "\n".join(cells) +
                         "} wires {" + "\n".join(wires) + "} control {" + control + "} }")


def _phase_parts(program) -> Phase:
    return ([cell.serialize() for cell in program.cells.values()],
            ["\n".join(wire.serialize() for wire in program.wires)],
            program.control.serialize())


def _rename(phase: Phase, names: dict[str, str]) -> Phase:
    program = _phase_program(phase)
    program.rename(names)
    return _phase_parts(program)


def _resize(phase: Phase, changes: dict[str, tuple[int, ...]], limits: dict[str, int] | None = None) -> Phase:
    """Resize declared arithmetic cells and their literal inputs in IR."""
    from scripts.pipeline.calyx_program import Expression
    program = _phase_program(phase)
    missing = set(changes) - set(program.cells)
    if missing:
        raise ValueError(f"schedule ABI changed: {sorted(missing)}")
    for name, parameters in changes.items():
        if not parameters or any(type(value) is not int or value <= 0 for value in parameters):
            raise ValueError("invalid arithmetic cell parameters")
        program.cells[name].parameters = list(parameters)
    bound_hits = {name: 0 for name in (limits or {})}
    for assignment in program.assignments():
        target = assignment.target
        if target.cell not in changes or target.port not in ("in", "left", "right"):
            continue
        parts = assignment.value.parts
        match = re.fullmatch(r"(\d+)'d(\d+)", parts[0]) if len(parts) == 1 and isinstance(parts[0], str) else None
        if match:
            value = int(match[2])
            if target.cell in (limits or {}) and target.port == "right":
                value = limits[target.cell]
                bound_hits[target.cell] += 1
            width = changes[target.cell][0]
            if not 0 <= value < (1 << width):
                raise ValueError(f"literal {value} does not fit {width}-bit cell {target.cell}")
            assignment.value = Expression.text(f"{width}'d{value}")
    if any(hits != 1 for hits in bound_hits.values()):
        raise ValueError("schedule loop bound ABI changed")
    return _phase_parts(program)


def _layer_norm(attention, prefix: str = "", final: bool = False,
                rows: int = 8, hidden: int = 64) -> Phase:
    if type(hidden) is not int or hidden not in (64, 128, 256, 512, 768):
        raise ValueError("LayerNorm hidden width must match a pinned TinyStories size")
    phase = attention._ln2_composed_phase() if prefix == "ln2_" else attention._ln1_composed_phase()
    flat_width = (rows * hidden - 1).bit_length()
    column_width = hidden.bit_length()
    column_address_width = (hidden - 1).bit_length()
    changes = {
        "row_counter": (rows.bit_length(),), "row_lt": (rows.bit_length(),), "increment_row": (rows.bit_length(),),
        "row_pad": (rows.bit_length(), flat_width), "row_shift": (flat_width,),
        "column_counter": (column_width,), "column_lt": (column_width,),
        "increment_column": (column_width,),
        "column_address": (column_width, column_address_width),
        "column_pad": (column_address_width, flat_width), "flat_address": (flat_width,),
    }
    phase = _resize(phase, {prefix + k: v for k, v in changes.items()},
                    {prefix + "row_lt": rows, prefix + "column_lt": hidden})
    from scripts.pipeline.calyx_passes import lower_layer_norm_geometry
    from scripts.pipeline.calyx_program import Group
    program = _phase_program(phase)
    lower_layer_norm_geometry(program, prefix=prefix, rows=rows, hidden=hidden)
    if final:
        # Namespace arithmetic and control nodes; reconnect the memory ABI.
        names = {name: "final_" + name for name in program.cells}
        names.update({wire.name: "final_" + wire.name for wire in program.wires if isinstance(wire, Group)})
        names.update({"block_input_q16_16": "block_output_q16_16", "ln1_gamma_q16_16": "final_gamma_q16_16", "ln1_beta_q16_16": "final_beta_q16_16", "ln1_output_q16_16": "final_output_q16_16"})
        program.rename(names)
    return _phase_parts(program)


def _causal(attention, *, split_denominator: bool = False, rows: int = 8,
            hidden: int = 64, head_dim: int = 4) -> Phase:
    if type(rows) is not int or rows not in (8, 20):
        raise ValueError("causal row capacity must be 8 or 20")
    if type(hidden) is not int or hidden not in (64, 128, 256, 512, 768) or head_dim != hidden // 16:
        raise ValueError("causal attention requires a pinned hidden width and sixteen matching heads")
    rw = rows.bit_length()
    vw = (rows * hidden - 1).bit_length()
    sw = (rows * rows * 16 - 1).bit_length()
    hw = (rows * 16 - 1).bit_length()
    lw = head_dim.bit_length()
    changes = {
        "causal_row": (rw,), "causal_key": (rw,), "causal_row_lt": (rw,),
        "causal_key_le": (rw,), "causal_row_increment": (rw,), "causal_key_increment": (rw,),
        "causal_lane": (lw,), "causal_lane_lt": (lw,), "causal_lane_increment": (lw,),
        "causal_row_pad8": (rw, vw), "causal_row_shift6": (vw,),
        "causal_key_pad8": (rw, vw), "causal_key_shift6": (vw,),
        "causal_head_pad8": (5, vw), "causal_head_shift2": (vw,),
        "causal_lane_pad8": (lw, vw), "causal_query_head_lane": (vw,),
        "causal_query_address": (vw,), "causal_key_address": (vw,),
        "causal_slot_head_key": (sw,), "causal_slot_address": (sw,),
        "causal_row_pad6": (rw, hw), "causal_row_shift4": (hw,),
        "causal_head_pad6": (5, hw), "causal_row_head_address": (hw,),
    }
    from scripts.pipeline.calyx_passes import lower_causal_geometry, split_attention_denominator
    phase = _resize(attention._causal_attention_phase(), changes,
                    {"causal_row_lt": rows, "causal_lane_lt": head_dim})
    program = _phase_program(phase)
    lower_causal_geometry(program, rows=rows, hidden=hidden, head_dim=head_dim)
    if split_denominator:
        split_attention_denominator(program)
    return _phase_parts(program)


def _block_phases(block, attention, mlp, *, narrow_products: bool = False,
                  requantization_width: int | None = None,
                  rows: int = 8, hidden: int = 64, mlp_width: int | None = None) -> list[Phase]:
    if mlp_width is None:
        mlp_width = 4 * hidden
    if type(hidden) is not int or hidden < 1 or type(mlp_width) is not int or mlp_width != 4 * hidden:
        raise ValueError("projection geometry requires a positive hidden width and 4x MLP width")
    zero_width = hidden.bit_length()
    zero_phase = _resize(
        attention._initialize_zero_bias_phase(),
        {"zero_bias_counter": (zero_width,), "zero_bias_lt": (zero_width,),
         "zero_bias_increment": (zero_width,),
         "zero_bias_address": (zero_width, (hidden - 1).bit_length())},
        {"zero_bias_lt": hidden},
    )
    phases = [_layer_norm(attention, rows=rows, hidden=hidden), zero_phase]
    def loop_geometry(phase, counter, comparison, increment, address, count):
        width = count.bit_length()
        return _resize(phase, {counter: (width,), comparison: (width,),
                              increment: (width,), address: (width, (count-1).bit_length())},
                       {comparison: count})
    def projection(prefix, source, inputs, outputs, bias, qdq=True):
        if qdq:
            phases.append(mlp._activation_qdq_phase(f"{prefix}_input_qdq", source, f"{prefix}_input_scale_q8_24", f"{prefix}_input_codes_i8", f"{prefix}_input_q16_16", rows * inputs, inputs, narrow_products=narrow_products))
        phases.append(mlp._gemv_phase(f"{prefix}_gemv", f"{prefix}_input_codes_i8", f"{prefix}_input_scale_q8_24", f"{prefix}_weight_codes_i8", f"{prefix}_accumulator_i64", rows, inputs, outputs, narrow_products=narrow_products, exact_trace_stride=True))
        phases.append(mlp._requantize_phase(f"{prefix}_requant", f"{prefix}_accumulator_i64", f"{prefix}_weight_scale_q8_24", bias, f"{prefix}_output_scale_q8_24", f"{prefix}_post_weight_rescale_bias_q16_16", f"{prefix}_output_codes_i8", f"{prefix}_output_q16_16", rows * outputs, outputs, narrow_products=narrow_products, requantization_width=requantization_width))
    for prefix in ("q", "k", "v"):
        projection(prefix, "ln1_output_q16_16", hidden, hidden, "zero_bias_q16_16")
    phases.append(loop_geometry(attention._initialize_causal_slots_phase(), "causal_zero_counter", "causal_zero_lt", "causal_zero_increment", "causal_zero_address", rows * rows * 16))
    phases.append(_causal(attention, split_denominator=narrow_products, rows=rows,
                          hidden=hidden, head_dim=hidden // 16))
    projection("out", "attention_context_q16_16", hidden, hidden, "out_bias_q16_16")
    phases.append(loop_geometry(attention._residual_add_phase(), "attention_residual_counter", "attention_residual_lt", "attention_residual_increment", "attention_residual_address", rows * hidden))
    phases.append(_layer_norm(attention, "ln2_", rows=rows, hidden=hidden))
    projection("c_fc", "ln2_output_q16_16", hidden, mlp_width, "c_fc_bias_q16_16")
    phases.append(loop_geometry(mlp._gelu_phase(), "gelu_entry_counter", "gelu_entry_lt", "gelu_increment_entry", "gelu_entry_address", rows * mlp_width))
    projection("c_proj", "gelu_output_q16_16", mlp_width, hidden, "c_proj_bias_q16_16")
    phases.append(loop_geometry(block._final_residual_phase(), "block_output_counter", "block_output_lt", "block_output_increment", "block_output_address", rows * hidden))
    return phases


def _memory(name: str, width: int, count: int) -> str:
    return f"@external {name} = seq_mem_d1({width}, {count}, {(count - 1).bit_length()});"


def _family_block_memory_count(name: str, original: int, *, sources: set[str],
                               rows: int, hidden: int) -> int:
    """Resize a proven block memory by its semantic axes, rejecting unknown ABI."""
    if name in {"attention_exp_lut_q1_20", "gelu_lut_q12"}:
        return original
    if name == "zero_bias_q16_16":
        return hidden
    if name in sources:
        if name.endswith("weight_codes_i8"):
            if original == 4096:
                return hidden * hidden
            if original == 16384:
                return 4 * hidden * hidden
        if original == 64:
            return hidden
        if original == 256:
            return 4 * hidden
    else:
        if name in {"attention_score_q8", "attention_delta_q8", "attention_exp_q1_20"}:
            return rows * rows * 16
        if name in {"attention_maximum_q8", "attention_denominator_q1_20"}:
            return rows * 16
        if original == 256:
            return rows * hidden
        if original == 1024:
            return rows * 4 * hidden
    raise ValueError(f"unclassified block memory geometry: {name} ({original})")


def _dense_strided_address(name: str, local_words: int, rows: int,
                           row_value: str, local_value: str,
                           output_name: str | None = None) -> tuple[list[str], list[str], str]:
    """Address dense rows with an exact constant stride, preserving power-of-two concat."""
    if type(local_words) is not int or local_words < 2 or type(rows) is not int or rows < 2:
        raise ValueError("dense address requires a positive local table and multiple rows")
    row_width = (rows - 1).bit_length()
    local_width = (local_words - 1).bit_length()
    total_width = (rows * local_words - 1).bit_length()
    output = output_name or name + "_bank_address"
    if local_words & (local_words - 1) == 0:
        return ([f"{output} = std_cat({row_width}, {local_width}, {total_width});"],
                [f"{output}.left = {row_value}; {output}.right = {local_value};"],
                f"{output}.out")
    pad = name + "_layer_pad"
    cells = [f"{pad} = std_pad({row_width}, {total_width});"]
    wires = [f"{pad}.in = {row_value};"]
    terms = []
    for bit in range(local_words.bit_length()):
        if not local_words & (1 << bit):
            continue
        shift = f"{name}_layer_shift_{bit}"
        cells.append(f"{shift} = std_lsh({total_width});")
        wires.append(f"{shift}.left = {pad}.out; {shift}.right = {total_width}'d{bit};")
        terms.append(f"{shift}.out")
    base = terms[0]
    for index, term in enumerate(terms[1:]):
        summation = f"{name}_layer_sum_{index}"
        cells.append(f"{summation} = std_add({total_width});")
        wires.append(f"{summation}.left = {base}; {summation}.right = {term};")
        base = f"{summation}.out"
    local_pad = name + "_local_pad"
    cells += [f"{local_pad} = std_pad({local_width}, {total_width});",
              f"{output} = std_add({total_width});"]
    wires += [f"{local_pad}.in = {local_value};",
              f"{output}.left = {base}; {output}.right = {local_pad}.out;"]
    return cells, wires, f"{output}.out"


def _layer_bank_address(name: str, local_words: int, layers: int,
                        layer_value: str, local_value: str) -> tuple[list[str], list[str], str]:
    if layers not in (4, 8):
        raise ValueError("layer-bank geometry requires four or eight layers")
    return _dense_strided_address(name, local_words, layers, layer_value, local_value)


def bram_bank_plan(width: int, count: int) -> dict[str, int]:
    """Return a legal XC7 BRAM36 geometry for a logical one-read memory.

    The Calyx ``seq_mem_d1`` declaration is a logical memory and its width
    and depth are not themselves legal RAMB36E1 configurations.  Keep this
    calculation in the compiler (rather than in a generated-SV annotation)
    so a later physical-memory pass can use one authenticated description for
    storage, address banking, and source-image serialization.
    """
    if type(width) is not int or type(count) is not int or width <= 0 or count <= 0:
        raise ValueError("memory width and count must be positive integers")
    # RAMB36E1 simple-dual-port legal width/depth pairs.  Widths are rounded
    # up to the next lane shape; unused high bits are padding, not data.
    shapes = ((1, 32768), (2, 16384), (4, 8192), (9, 4096),
              (18, 2048), (36, 1024), (72, 512))
    for physical_width, physical_depth in shapes:
        if width <= physical_width:
            banks = (count + physical_depth - 1) // physical_depth
            return {
                "logical_width": width,
                "logical_words": count,
                "banks": banks,
                "physical_width": physical_width,
                "physical_depth": physical_depth,
                "logical_words_per_bank": physical_depth,
                "capacity_bits": banks * physical_width * physical_depth,
            }
    raise ValueError(f"memory width {width} exceeds RAMB36E1 72-bit lane")


def projection_weight_bank_plan() -> dict[str, dict[str, int | str]]:
    """Describe the explicit layer-bank geometry for projection code memories.

    The generated Calyx currently uses a logical layer-prefixed memory.  Keep
    this contract in the compiler so a physical-memory pass can materialize
    banks without guessing tensor dimensions or changing address semantics.
    ``local_words`` is the per-layer table depth; ``bank_shift`` is the number
    of low address bits retained inside each layer bank.
    """
    geometry = {
        "q_weight_codes_i8": 4096,
        "k_weight_codes_i8": 4096,
        "v_weight_codes_i8": 4096,
        "out_weight_codes_i8": 4096,
        "c_fc_weight_codes_i8": 16384,
        "c_proj_weight_codes_i8": 16384,
    }
    return {
        name: {
            "layers": 8,
            "local_words": words,
            "bank_shift": words.bit_length() - 1,
            "address_mapping": "model_layer_bank:local_weight_address",
        }
        for name, words in geometry.items()
    }


def projection_weight_packing_plan() -> dict[str, int | str]:
    """Return the exact storage ABI for packed projection INT8 codes."""
    return {
        "lanes": 4,
        "word_width": 32,
        "lane_order": "little_endian_int8",
        "row_major": "preserved",
        "quantization_changed": False,
    }


def _structured_input(value):
    from scripts.pipeline.calyx_program import Program
    return (value, False) if isinstance(value, Program) else (Program.parse(value), True)


def alias_runtime_memories(futil, aliases: dict[str, str]):
    """Coalesce an explicit, schedule-justified alias set in structured IR."""
    program, serialized = _structured_input(futil)
    program.alias_memories(aliases)
    return program.serialize() if serialized else program


def narrow_source_memory_words(futil, names: list[str], target_width: int = 32):
    """Represent source storage narrowing with explicit read adapters."""
    program, serialized = _structured_input(futil)
    for name in names:
        program.narrow_memory(name, target_width, writable=False)
    return program.serialize() if serialized else program


def narrow_runtime_memory_words(futil, names: list[str]):
    """Represent runtime storage narrowing with read/write adapters."""
    program, serialized = _structured_input(futil)
    for name in names:
        program.narrow_memory(name, 32, writable=True)
    return program.serialize() if serialized else program


def _source_binding(name: str) -> tuple[str, str]:
    if name.startswith("ln"):
        ln, kind, _ = name.split("_", 2)
        return "parameter", f"blocks.{{layer}}.{ln}.{'weight' if kind == 'gamma' else 'bias'}"
    for prefix, projection, module in (("q", "attn.q", "attn.attention.q_proj"), ("k", "attn.k", "attn.attention.k_proj"), ("v", "attn.v", "attn.attention.v_proj"), ("out", "attn.out", "attn.attention.out_proj"), ("c_fc", "mlp.fc", "mlp.c_fc"), ("c_proj", "mlp.proj", "mlp.c_proj")):
        if name.startswith(prefix + "_"):
            suffix = name[len(prefix) + 1:]
            if suffix == "weight_codes_i8": return "code", f"blocks.{{layer}}.{projection}.weight"
            if suffix == "weight_scale_q8_24": return "scale", f"blocks.{{layer}}.{projection}.weight"
            if suffix == "bias_q16_16": return "parameter", f"blocks.{{layer}}.{projection}.bias"
            if suffix in ("input_scale_q8_24", "output_scale_q8_24"):
                return "scale", f"activation::transformer.h.{{layer}}.{module}.{suffix.split('_')[0]}"
    raise ValueError(f"unknown parameter source: {name}")


class Builder:
    def __init__(self):
        self.cells: list[str] = []
        self.wires: list[str] = []

    def cell(self, name, primitive, *args):
        self.cells.append(f"{name} = {primitive}({', '.join(map(str, args))});")
        return name

    def group(self, name, assignments, done):
        self.wires.append(f"group {name} {{\n" + "\n".join(assignments) + f"\n{name}[done] = {done};\n}}")
        return name + ";"

    def counter(self, name, width, count):
        self.cell(name, "std_reg", width)
        self.cell(name + "_lt", "std_lt", width)
        self.cell(name + "_add", "std_add", width)
        self.wires.append(f"comb group {name}_condition {{ {name}_lt.left = {name}.out; {name}_lt.right = {count}; }}")
        self.group(name + "_init", [f"{name}.in = {width}'d0;", f"{name}.write_en = 1'd1;"], name + ".done")
        self.group(name + "_increment", [f"{name}_add.left = {name}.out;", f"{name}_add.right = {width}'d1;", f"{name}.in = {name}_add.out;", f"{name}.write_en = 1'd1;"], name + ".done")

    def loop(self, name, body):
        return f"{name}_init; while {name}_lt.out with {name}_condition {{ seq {{ {body} {name}_increment; }} }}"

    def read(self, name, memories):
        assignments = []
        for mem, address in memories:
            assignments += [f"{mem}.addr0 = {address};", f"{mem}.content_en = 1'd1;"]
        done = " & ".join(f"{mem}.done" for mem, _ in memories)
        return self.group(name, assignments, f"({done}) ? 1'd1")

    def write(self, name, memories):
        assignments = []
        for mem, address, value in memories:
            assignments += [f"{mem}.addr0 = {address};", f"{mem}.content_en = 1'd1;", f"{mem}.write_en = 1'd1;", f"{mem}.write_data = {value};"]
        done = " & ".join(f"{mem}.done" for mem, _, _ in memories)
        return self.group(name, assignments, f"({done}) ? 1'd1")

    def round(self, name, value, shift):
        for suffix, op in (("negative", "std_slt"), ("negate", "std_ssub"), ("absolute", "std_mux"), ("bias", "std_add"), ("shift", "std_rsh"), ("signed", "std_ssub"), ("select", "std_mux"), ("result", "std_reg")):
            self.cell(name + "_" + suffix, op, 64)
        return self.group(name, [f"{name}_negative.left = {value};", f"{name}_negative.right = 64'd0;", f"{name}_negate.left = 64'd0;", f"{name}_negate.right = {value};", f"{name}_absolute.cond = {name}_negative.out;", f"{name}_absolute.tru = {name}_negate.out;", f"{name}_absolute.fal = {value};", f"{name}_bias.left = {name}_absolute.out;", f"{name}_bias.right = 64'd{1 << (shift - 1)};", f"{name}_shift.left = {name}_bias.out;", f"{name}_shift.right = 64'd{shift};", f"{name}_signed.left = 64'd0;", f"{name}_signed.right = {name}_shift.out;", f"{name}_select.cond = {name}_negative.out;", f"{name}_select.tru = {name}_signed.out;", f"{name}_select.fal = {name}_shift.out;", f"{name}_result.in = {name}_select.out;", f"{name}_result.write_en = 1'd1;"], name + "_result.done")


def _banked_read(builder: Builder, group_name: str, prefix: str, memory_prefix: str,
                 banks: int, bank_address: str, local_address: str, width: int,
                 packed_lanes: int | None = None, lane_address: str | None = None,
                 lane_width: int = 16):
    """Read a depth-bounded banked table and mux its selected word.

    All physical banks are read in parallel.  The seven-bit bank selector is
    reduced through a balanced mux tree; unused leaves are constant zero.  The
    caller receives a Calyx group statement and the mux output expression.
    """
    leaf_count = 1 << (banks - 1).bit_length()
    bank_bits = leaf_count.bit_length() - 1
    for level in range(bank_bits):
        # Select the actual level bit.  A plain std_slice would always select
        # bit zero and would alias every bank except the first pair.
        builder.cell(f"{prefix}_bank_bit_{level}", "std_bit_slice", bank_bits, level, level, 1)
        builder.wires.append(f"{prefix}_bank_bit_{level}.in = {bank_address};")
    leaves = []
    assignments = []
    done_terms = []
    for bank in range(banks):
        name = f"{memory_prefix}_b{bank}"
        assignments += [f"{name}.addr0 = {local_address};", f"{name}.content_en = 1'd1;"]
        done_terms.append(f"{name}.done")
        leaves.append(f"{name}.read_data")
    leaves += [f"{width}'d0"] * (leaf_count - banks)
    for level in range(bank_bits):
        next_leaves = []
        for index in range(0, len(leaves), 2):
            mux = f"{prefix}_bank_mux_{level}_{index // 2}"
            builder.cell(mux, "std_mux", width)
            builder.wires.append(
                f"{mux}.cond = {prefix}_bank_bit_{level}.out; "
                f"{mux}.tru = {leaves[index + 1]}; {mux}.fal = {leaves[index]};"
            )
            next_leaves.append(f"{mux}.out")
        leaves = next_leaves
    value = leaves[0]
    if packed_lanes is not None:
        if packed_lanes != 8 or width != 64 or lane_address is None:
            raise ValueError("packed banked reads require 64-bit words, eight lanes, and a lane address")
        lane = f"{prefix}_lane"
        lane_pad = f"{prefix}_lane_pad"
        lane_shift = f"{prefix}_lane_shift"
        code_shift = f"{prefix}_code_shift"
        code_slice = f"{prefix}_code_slice"
        builder.cell(lane, "std_slice", lane_width, 3)
        builder.cell(lane_pad, "std_pad", 3, 64)
        builder.cell(lane_shift, "std_lsh", 64)
        builder.cell(code_shift, "std_rsh", 64)
        builder.cell(code_slice, "std_slice", 64, 8)
        builder.wires.append(
            f"{lane}.in = {lane_address}; "
            f"{lane_pad}.in = {lane}.out; "
            f"{lane_shift}.left = {lane_pad}.out; "
            f"{lane_shift}.right = 64'd3; "
            f"{code_shift}.left = {value}; "
            f"{code_shift}.right = {lane_shift}.out; "
            f"{code_slice}.in = {code_shift}.out;"
        )
        value = f"{code_slice}.out"
    group = builder.group(group_name, assignments, f"({' & '.join(done_terms)}) ? 1'd1")
    return group, value


def _orchestration(builder: Builder, block_control: str, final_control: str, lm_control: str, *, audit_checkpoints: bool = True, stream_lm_head: bool = False, stream_gemv_control: str | None = None, board_io: bool = False, generation_steps: int = 2, layers: int = 8, hidden: int = 64, vocab: int = 50257) -> str:
    b = builder
    dense_addresses: dict[str, tuple[str, str, list[str]]] = {}
    if type(layers) is not int or layers not in (4, 8):
        raise ValueError("model orchestration requires a pinned four- or eight-layer schedule")
    geometry = _load(ROOT / "scripts/pipeline/tinystories_generation_geometry.py", "orchestration_geometry").generation_geometry(generation_steps)
    rows, rw, sw = geometry.row_capacity, geometry.row_counter_width, geometry.step_counter_width
    aw, iw = geometry.row_address_width, geometry.token_index_width
    count = rows * hidden
    cw, vw = count.bit_length(), (count - 1).bit_length()
    column_width = hidden.bit_length()
    column_address_width = (hidden - 1).bit_length()
    token_plan = build_token_code_plan(vocab=vocab, hidden=hidden,
                                       bank_depth=TOKEN_CODE_BANK_DEPTH)
    token_row_bits = (token_plan.rows_per_bank - 1).bit_length()
    token_bank_bits = (token_plan.bank_count - 1).bit_length()
    layer_width, layer_bank_width = layers.bit_length(), (layers - 1).bit_length()
    counter_specs = [("model_step", sw, f"{sw}'d{generation_steps}"), ("model_layer", layer_width, f"{layer_width}'d{layers}"), ("model_row", rw, f"{rw}'d{rows}"), ("model_column", column_width, f"{column_width}'d{hidden}"), ("model_copy", cw, f"{cw}'d{count}"), ("model_logit", 16, f"16'd{vocab}")]
    if stream_lm_head:
        counter_specs.pop()  # GEMV's output counter owns the vocabulary index.
    if not stream_lm_head:
        counter_specs.insert(-1, ("model_logit_row", 4, "model_context_length.out"))
    for name, width, limit in counter_specs:
        b.counter(name, width, limit)
    b.cell("model_context_length", "std_reg", rw)
    b.cell("model_context_next", "std_add", rw)
    if stream_lm_head:
        b.cell("model_last_row", "std_sub", rw)
    b.cell("model_context_address", "std_slice", rw, aw)
    b.cell("model_layer_bank", "std_slice", layer_width, layer_bank_width)
    b.cell("model_step_address", "std_slice", sw, iw)
    if board_io and generation_steps <= 2:
        b.cell("model_step_is_first", "std_not", 1)
        b.cell("model_token0_next", "std_mux", 16)
        b.cell("model_token1_next", "std_mux", 16)
        b.cell("model_token0", "std_reg", 16)
        b.cell("model_token1", "std_reg", 16)
        b.cell("model_token_select", "std_mux", 16)
    if board_io and generation_steps == 16:
        for index in range(16):
            b.cell(f"model_step_eq{index}", "std_eq", iw)
            b.cell(f"model_output_eq{index}", "std_eq", iw)
            b.cell(f"model_token{index}_next", "std_mux", 16)
            b.cell(f"model_token{index}", "std_reg", 16)
            b.cell(f"model_output_mux{index}", "std_mux", 16)
            previous = "16'd0" if index == 0 else f"model_output_mux{index-1}.out"
            b.wires.append(f"model_step_eq{index}.left = model_step_address.out; model_step_eq{index}.right = {iw}'d{index}; model_token{index}_next.cond = model_step_eq{index}.out; model_token{index}_next.tru = model_best_token.out; model_token{index}_next.fal = model_token{index}.out; model_output_eq{index}.left = token_index; model_output_eq{index}.right = {iw}'d{index}; model_output_mux{index}.cond = model_output_eq{index}.out; model_output_mux{index}.tru = model_token{index}.out; model_output_mux{index}.fal = {previous};")
        b.wires.append("token_id = model_output_mux15.out;")
    b.cell("model_row_address", "std_slice", rw, aw)
    b.cell("model_prompt_address", "std_slice", rw, 2)
    if board_io:
        for index in range(4):
            b.cell(f"model_prompt_eq{index}", "std_eq", 2)
        b.cell("model_prompt_pair01", "std_or", 1)
        b.cell("model_prompt_value01", "std_mux", 16)
        b.cell("model_prompt_value23", "std_mux", 16)
        b.cell("model_prompt_value", "std_mux", 16)
    b.cell("model_is_prompt", "std_lt", rw)
    b.cell("model_position_address", "std_pad", rw, 5)
    b.cell("model_column_address", "std_slice", column_width, column_address_width)
    if board_io:
        b.cell("model_token_column_shift", "std_rsh", column_address_width)
        b.cell("model_token_column_word", "std_slice", column_address_width,
               column_address_width - 3)
    if hidden & (hidden - 1):
        for name, row_count, row_value in (
            ("model_hidden_address", rows, "model_row_address.out"),
            ("model_token_address", vocab, "context_tokens.read_data"),
            ("model_position_weight_address", 32, "model_position_address.out"),
        ):
            cells, wires, _ = _dense_strided_address(
                name, hidden, row_count, row_value, "model_column_address.out",
                output_name=name,
            )
            b.cells += cells
            dense_addresses[name] = (row_value, "model_column_address.out", wires)
    else:
        b.cell("model_hidden_address", "std_cat", aw, column_address_width, vw)
        b.cell("model_token_address", "std_cat", 16, column_address_width,
               16 + column_address_width)
        b.cell("model_position_weight_address", "std_cat", 5, column_address_width,
               5 + column_address_width)
    b.cell("model_copy_address", "std_slice", cw, vw)
    token_code_read = None
    token_scale_read = None
    token_code_value = "token_weight_codes_i8.read_data"
    token_scale_value = "token_weight_scale_q8_24.read_data"
    if board_io:
        b.cell("model_token_row_low", "std_slice", 16, token_row_bits)
        b.cell("model_token_bank_full", "std_rsh", 16)
        b.cell("model_token_scale_bank_full", "std_rsh", 16)
        b.cell("model_token_bank", "std_slice", 16, token_bank_bits)
        b.cell("model_token_scale_bank", "std_slice", 16, 5)
        if token_plan.words_per_row & (token_plan.words_per_row - 1):
            cells, wires, _ = _dense_strided_address(
                "model_token_code", token_plan.words_per_row,
                token_plan.rows_per_bank, "model_token_row_low.out",
                "model_token_column_word.out", output_name="model_token_code_local"
            )
            b.cells += cells
            dense_addresses["model_token_code_local"] = (
                "model_token_row_low.out", "model_token_column_word.out", wires
            )
        else:
            b.cell("model_token_code_local", "std_cat", token_row_bits,
                   column_address_width - 3, 14)
        b.cell("model_token_scale_local", "std_slice", 16, 11)
        b.wires += [
            "model_token_row_low.in = context_tokens.read_data;",
            "model_token_bank_full.left = context_tokens.read_data;",
            f"model_token_bank_full.right = 16'd{token_row_bits};",
            "model_token_scale_bank_full.left = context_tokens.read_data;",
            "model_token_scale_bank_full.right = 16'd11;",
            "model_token_bank.in = model_token_bank_full.out;",
            "model_token_scale_bank.in = model_token_scale_bank_full.out;",
            "model_token_column_shift.left = model_column_address.out;",
            f"model_token_column_shift.right = {column_address_width}'d3;",
            "model_token_column_word.in = model_token_column_shift.out;",
            "model_token_code_local.left = model_token_row_low.out;",
            "model_token_code_local.right = model_token_column_word.out;",
            "model_token_scale_local.in = context_tokens.read_data;",
        ]
        token_scale_read, token_scale_value = _banked_read(
            b, "model_read_token_scale", "model_token_scale", "token_weight_scale_q8_24",
            TOKEN_SCALE_BANKS, "model_token_scale_bank.out", "model_token_scale_local.out", 32)
        token_code_read, token_code_value = _banked_read(
            b, "model_read_token_codes", "model_token_code", "token_weight_codes_packed",
            token_plan.bank_count, "model_token_bank.out", "model_token_code_local.out", 64,
            packed_lanes=PACKED_TOKEN_CODE_LANES, lane_address="model_column_address.out", lane_width=column_address_width)
    if audit_checkpoints:
        b.cell("model_embedding_checkpoint_address", "std_cat", iw, vw, iw + vw)
        b.cell("model_block_step_layer", "std_cat", iw, layer_bank_width, iw + layer_bank_width)
        b.cell("model_block_checkpoint_address", "std_cat", iw + layer_bank_width, vw, iw + layer_bank_width + vw)
        b.cell("model_ln_checkpoint_address", "std_cat", iw, vw, iw + vw)
    b.cell("model_valid_not", "std_not", 1)
    b.cell("model_wait_register", "std_reg", 1)
    if audit_checkpoints:
        b.wires += ["""
model_layer_bank.in = model_layer.out;
model_step_address.in = model_step.out;
model_context_address.in = model_context_length.out;
model_row_address.in = model_row.out;
model_prompt_address.in = model_row.out;
model_position_address.in = model_row.out;
model_column_address.in = model_column.out;
model_hidden_address.left = model_row_address.out;
model_hidden_address.right = model_column_address.out;
model_token_address.left = context_tokens.read_data;
model_token_address.right = model_column_address.out;
model_position_weight_address.left = model_position_address.out;
model_position_weight_address.right = model_column_address.out;
model_copy_address.in = model_copy.out;
model_embedding_checkpoint_address.left = model_step_address.out;
model_embedding_checkpoint_address.right = model_hidden_address.out;
model_block_step_layer.left = model_step_address.out;
model_block_step_layer.right = model_layer_bank.out;
model_block_checkpoint_address.left = model_block_step_layer.out;
model_block_checkpoint_address.right = model_copy_address.out;
model_ln_checkpoint_address.left = model_step_address.out;
model_ln_checkpoint_address.right = model_copy_address.out;
comb group model_wait_condition { model_valid_not.in = valid; }
comb group model_prompt_condition { model_is_prompt.left = model_row.out; model_is_prompt.right = 4'd4; }
"""]
    else:
        b.wires += ["""
model_layer_bank.in = model_layer.out;
model_step_address.in = model_step.out;
model_context_address.in = model_context_length.out;
model_row_address.in = model_row.out;
model_prompt_address.in = model_row.out;
model_position_address.in = model_row.out;
model_column_address.in = model_column.out;
model_hidden_address.left = model_row_address.out;
model_hidden_address.right = model_column_address.out;
model_token_address.left = context_tokens.read_data;
model_token_address.right = model_column_address.out;
model_position_weight_address.left = model_position_address.out;
model_position_weight_address.right = model_column_address.out;
model_copy_address.in = model_copy.out;
comb group model_wait_condition { model_valid_not.in = valid; }
comb group model_prompt_condition { model_is_prompt.left = model_row.out; model_is_prompt.right = 4'd4; }
"""]
    if board_io:
        b.wires.append(" ".join(
            [f"model_prompt_eq{index}.left = model_prompt_address.out; model_prompt_eq{index}.right = 2'd{index};"
             for index in range(4)]
            + [
                "model_prompt_pair01.left = model_prompt_eq0.out; model_prompt_pair01.right = model_prompt_eq1.out;",
                "model_prompt_value01.cond = model_prompt_eq0.out; model_prompt_value01.tru = prompt_0; model_prompt_value01.fal = prompt_1;",
                "model_prompt_value23.cond = model_prompt_eq2.out; model_prompt_value23.tru = prompt_2; model_prompt_value23.fal = prompt_3;",
                "model_prompt_value.cond = model_prompt_pair01.out; model_prompt_value.tru = model_prompt_value01.out; model_prompt_value.fal = model_prompt_value23.out;",
            ]
        ))
    wait = b.group("model_wait", ["model_wait_register.in = 1'd1;", "model_wait_register.write_en = 1'd1;"], "model_wait_register.done")
    init_length = b.group("model_init_length", [f"model_context_length.in = {rw}'d4;", "model_context_length.write_en = 1'd1;"], "model_context_length.done")
    read_prompt = b.read("model_read_prompt", [("prompt_tokens", "model_prompt_address.out")])
    copy_prompt = b.write("model_copy_prompt", [("context_tokens", "model_row_address.out", "model_prompt_value.out" if board_io else "prompt_tokens.read_data")])
    pad_prompt = b.write("model_pad_prompt", [("context_tokens", "model_row_address.out", "16'd0")])
    prompt = b.loop("model_row", f"if model_is_prompt.out with model_prompt_condition {{ seq {{ {read_prompt} {copy_prompt} }} }} else {{ {pad_prompt} }}")
    read_token = b.read("model_read_token", [("context_tokens", "model_row_address.out"), ("position_weight_scale_q8_24", "model_position_address.out")])
    if board_io:
        read_codes_position = b.read("model_read_position_code", [("position_weight_codes_i8", "model_position_weight_address.out")])
        read_token_scale = token_scale_read
        read_codes = token_code_read + read_codes_position
    else:
        read_token_scale = b.read("model_read_token_scale", [("token_weight_scale_q8_24", "context_tokens.read_data")])
        read_codes = b.read("model_read_embedding_codes", [("token_weight_codes_i8", "model_token_address.out"), ("position_weight_codes_i8", "model_position_weight_address.out")])
        token_scale_value = "token_weight_scale_q8_24.read_data"
        token_code_value = "token_weight_codes_i8.read_data"
    token_scale_assign = ""
    if board_io:
        b.cell("model_token_scale_signed", "std_signext", 32, 64)
        token_scale_assign = f"model_token_scale_signed.in = {token_scale_value};"
        token_scale_value = "model_token_scale_signed.out"
    for p in ("model_token", "model_position"):
        b.cell(p + "_signed", "std_signext", 8, 64)
        b.cell(p + "_product", "std_smult_pipe", 64)
    multiply_assignments = [f"model_token_signed.in = {token_code_value};", "model_token_product.left = model_token_signed.out;", f"model_token_product.right = {token_scale_value};", "model_token_product.go = 1'd1;", "model_position_signed.in = position_weight_codes_i8.read_data;", "model_position_product.left = model_position_signed.out;", "model_position_product.right = position_weight_scale_q8_24.read_data;", "model_position_product.go = 1'd1;"]
    if token_scale_assign:
        multiply_assignments.insert(0, token_scale_assign)
    multiply = b.group("model_embedding_multiply", multiply_assignments, "(model_token_product.done & model_position_product.done) ? 1'd1")
    round_token = b.round("model_round_token", "model_token_product.out", 8)
    round_position = b.round("model_round_position", "model_position_product.out", 8)
    b.cell("model_embedding_sum", "std_sadd", 64)
    b.wires.append("model_embedding_sum.left = model_round_token_result.out; model_embedding_sum.right = model_round_position_result.out;")
    embedding_writes = [("block_input_q16_16", "model_hidden_address.out", "model_embedding_sum.out")]
    if audit_checkpoints:
        embedding_writes += [("model_embedding", "model_embedding_checkpoint_address.out", "model_embedding_sum.out"), ("model_token_embedding", "model_embedding_checkpoint_address.out", "model_round_token_result.out"), ("model_position_embedding", "model_embedding_checkpoint_address.out", "model_round_position_result.out")]
    write_embedding = b.write("model_write_embedding", embedding_writes)
    embedding = b.loop("model_row", read_token + read_token_scale + b.loop("model_column", read_codes + multiply + round_token + round_position + write_embedding))
    read_block = b.read("model_read_block", [("block_output_q16_16", "model_copy_address.out")])
    block_writes = [("block_input_q16_16", "model_copy_address.out", "block_output_q16_16.read_data")]
    if audit_checkpoints:
        block_writes += [("model_block_outputs", "model_block_checkpoint_address.out", "block_output_q16_16.read_data")]
    copy_block = b.write("model_copy_block", block_writes)
    blocks = b.loop("model_layer", block_control + b.loop("model_copy", read_block + copy_block))
    read_ln = b.read("model_read_final_ln", [("final_output_q16_16", "model_copy_address.out")]) if audit_checkpoints else ""
    copy_ln = ""
    if audit_checkpoints:
        copy_ln = b.write("model_copy_final_ln", [("model_final_ln", "model_ln_checkpoint_address.out", "final_output_q16_16.read_data")])
    # Vocabulary rows are padded to stride 65536 only for addressing; all
    # 50257 exact vocabulary entries, and only real context rows, are visited.
    if not stream_lm_head:
        b.cell("model_logit_row_address", "std_slice", 4, 3)
    if not stream_lm_head:
        b.cell("model_accumulator_address", "std_cat", 3, 16, 19)
    if audit_checkpoints and not stream_lm_head:
        b.cell("model_logits_address", "std_cat", 1, 19, 20)
    b.cell("model_logit_product", "std_smult_pipe", 64)
    b.cell("model_best_value", "std_reg", 64)
    b.cell("model_best_token", "std_reg", 16)
    b.cell("model_logit_is_better", "std_sgt", 64)
    if audit_checkpoints and not stream_lm_head:
        b.wires += ["""
model_logit_row_address.in = model_logit_row.out;
model_accumulator_address.left = model_logit_row_address.out;
model_accumulator_address.right = model_logit.out;
model_logits_address.left = model_step_address.out;
model_logits_address.right = model_accumulator_address.out;
comb group model_greedy_condition { model_logit_is_better.left = model_round_logit_result.out; model_logit_is_better.right = model_best_value.out; }
"""]
    else:
        b.wires += ["""
model_last_row.left = model_context_length.out;
model_last_row.right = 4'd1;
comb group model_greedy_condition { model_logit_is_better.left = model_round_logit_result.out; model_logit_is_better.right = model_best_value.out; }
"""]
    if board_io and generation_steps <= 2:
        b.wires.append("model_step_is_first.in = model_step_address.out; model_token0_next.cond = model_step_is_first.out; model_token0_next.tru = model_best_token.out; model_token0_next.fal = model_token0.out; model_token1_next.cond = model_step_address.out; model_token1_next.tru = model_best_token.out; model_token1_next.fal = model_token1.out; model_token_select.cond = token_index; model_token_select.tru = model_token1.out; model_token_select.fal = model_token0.out; token_id = model_token_select.out;")
    logit_index = "lm_gemv_output_counter.out" if stream_lm_head else "model_logit.out"
    init_greedy = b.group("model_init_greedy", ["model_best_value.in = 64'd9223372036854775808;", "model_best_value.write_en = 1'd1;", "model_best_token.in = 16'd0;", "model_best_token.write_en = 1'd1;"], "(model_best_value.done & model_best_token.done) ? 1'd1")
    logit_scale_read = None
    logit_scale_value = "token_weight_scale_q8_24.read_data"
    if board_io:
        b.cell("model_logit_scale_bank_full", "std_rsh", 16)
        b.cell("model_logit_scale_bank", "std_slice", 16, 5)
        b.cell("model_logit_scale_local", "std_slice", 16, 11)
        b.wires += [
            f"model_logit_scale_bank_full.left = {logit_index};",
            "model_logit_scale_bank_full.right = 16'd11;",
            "model_logit_scale_bank.in = model_logit_scale_bank_full.out;",
            f"model_logit_scale_local.in = {logit_index};",
        ]
        logit_scale_read, logit_scale_value = _banked_read(
            b, "model_read_logit_scale", "model_logit_scale", "token_weight_scale_q8_24",
            TOKEN_SCALE_BANKS, "model_logit_scale_bank.out", "model_logit_scale_local.out", 32)
    if stream_lm_head:
        read_logit = (logit_scale_read if board_io else b.read("model_read_logit", [("token_weight_scale_q8_24", logit_index)]))
        logit_value = "lm_gemv_accumulator.out"
    else:
        if board_io:
            read_logit = b.read("model_read_logit", [("lm_accumulator_i64", "model_accumulator_address.out")]) + logit_scale_read
        else:
            read_logit = b.read("model_read_logit", [("lm_accumulator_i64", "model_accumulator_address.out"), ("token_weight_scale_q8_24", "model_logit.out")])
        logit_value = "lm_accumulator_i64.read_data"
    logit_scale_assign = ""
    if board_io:
        logit_scale_assign = f"model_token_scale_signed.in = {logit_scale_value};"
        logit_scale_value = "model_token_scale_signed.out"
    logit_assignments = [f"model_logit_product.left = {logit_value};", f"model_logit_product.right = {logit_scale_value};", "model_logit_product.go = 1'd1;"]
    if logit_scale_assign:
        logit_assignments.insert(0, logit_scale_assign)
    mul_logit = b.group("model_multiply_logit", logit_assignments, "model_logit_product.done")
    round_logit = b.round("model_round_logit", "model_logit_product.out", 32)
    write_logit = ""
    if audit_checkpoints and not stream_lm_head:
        write_logit = b.write("model_write_logit", [("model_logits", "model_logits_address.out", "model_round_logit_result.out")])
    greedy = b.group("model_update_greedy", ["model_best_value.in = model_round_logit_result.out;", "model_best_value.write_en = 1'd1;", f"model_best_token.in = {logit_index};", "model_best_token.write_en = 1'd1;"], "(model_best_value.done & model_best_token.done) ? 1'd1")
    consume_logit = read_logit + mul_logit + round_logit + write_logit + f"if model_logit_is_better.out with model_greedy_condition {{ {greedy} }}"
    if stream_lm_head:
        # Consume each scalar accumulator before GEMV advances to the next
        # vocabulary entry and resets it. No logits memory is materialized.
        if stream_gemv_control is None:
            raise ValueError("streamed LM head requires a GEMV control section")
        advance = "lm_gemv_increment_output_counter;"
        if stream_gemv_control.count(advance) != 1:
            raise ValueError("streamed LM head requires one output advance boundary")
        streamed = stream_gemv_control.replace(advance, consume_logit + advance)
        logits = lm_control + init_greedy + streamed
        lm_control = ""
    else:
        logit_scan = b.loop("model_logit", consume_logit)
        logits = b.loop("model_logit_row", init_greedy + logit_scan)
    if board_io and generation_steps <= 2:
        feedback = b.group("model_commit_feedback", [
            "selected_tokens.addr0 = model_step_address.out;", "selected_tokens.content_en = 1'd1;", "selected_tokens.write_en = 1'd1;", "selected_tokens.write_data = model_best_token.out;",
            "context_tokens.addr0 = model_context_address.out;", "context_tokens.content_en = 1'd1;", "context_tokens.write_en = 1'd1;", "context_tokens.write_data = model_best_token.out;",
            "model_token0.in = model_token0_next.out;", "model_token0.write_en = 1'd1;", "model_token1.in = model_token1_next.out;", "model_token1.write_en = 1'd1;",
        ], "(selected_tokens.done & context_tokens.done) ? 1'd1")
    elif board_io:
        feedback = b.group("model_commit_feedback", [
            "selected_tokens.addr0 = model_step_address.out;", "selected_tokens.content_en = 1'd1;", "selected_tokens.write_en = 1'd1;", "selected_tokens.write_data = model_best_token.out;",
            "context_tokens.addr0 = model_context_address.out;", "context_tokens.content_en = 1'd1;", "context_tokens.write_en = 1'd1;", "context_tokens.write_data = model_best_token.out;",
            *[f"model_token{index}.in = model_token{index}_next.out;" + " model_token%d.write_en = 1'd1;" % index for index in range(16)],
        ], "(selected_tokens.done & context_tokens.done) ? 1'd1")
    else:
        feedback = b.write("model_commit_feedback", [("selected_tokens", "model_step_address.out", "model_best_token.out"), ("context_tokens", "model_context_address.out", "model_best_token.out")])
    next_context = b.group("model_next_context", ["model_context_next.left = model_context_length.out;", f"model_context_next.right = {rw}'d1;", "model_context_length.in = model_context_next.out;", "model_context_length.write_en = 1'd1;"], "model_context_length.done")
    final_copy = b.loop("model_copy", read_ln + copy_ln) if audit_checkpoints else ""
    steps = b.loop("model_step", embedding + blocks + final_control + final_copy + lm_control + logits + feedback + next_context)
    if dense_addresses:
        text = "\n".join(b.wires)
        for name, (row_value, local_value, address_wires) in dense_addresses.items():
            for obsolete in (f"{name}.left = {row_value};",
                             f"{name}.right = {local_value};"):
                if text.count(obsolete) != 1:
                    raise ValueError(f"{name}: dense-address handoff ABI changed")
                text = text.replace(obsolete, "", 1)
            text += "\n" + "\n".join(address_wires)
        b.wires = [text]
    if generation_steps == 16:
        b.wires = [wire.replace("model_is_prompt.right = 4'd4;", "model_is_prompt.right = 5'd4;").replace("model_last_row.right = 4'd1;", "model_last_row.right = 5'd1;") for wire in b.wires]
    return f"seq {{ while model_valid_not.out with model_wait_condition {{ {wait} }} {init_length} {prompt} {steps} }}"


def generate_model_kernel(oracle_path: Path = ORACLE, *, host_second_token=None, host_intermediate_states=None, production: bool = False, generation_steps: int = 2, audit_checkpoints: bool | None = None, model_spec: FamilyModelSpec | None = None) -> CalyxArtifact:
    if type(generation_steps) is not int or generation_steps not in (1, 2, 16):
        raise ValueError("generation_steps must be 1, 2, or 16 (authenticated oracle prefix)")
    if host_second_token is not None:
        raise ValueError("host_second_token_forbidden")
    if audit_checkpoints is None:
        audit_checkpoints = not production
    if host_intermediate_states is not None:
        raise ValueError("host_intermediate_states_forbidden")
    geometry = _load(ROOT / "scripts/pipeline/tinystories_generation_geometry.py", "model_generation_geometry").generation_geometry(generation_steps)
    if model_spec is not None:
        if not isinstance(model_spec, FamilyModelSpec):
            raise TypeError("model_spec must be a validated FamilyModelSpec")
        oracle = json.loads(Path(oracle_path).read_text())
        identity = {"schema": "task5-tinystories-fixed-point-family-oracle-v1",
                    "revision": model_spec.revision,
                    "tokenizer_identity": model_spec.tokenizer_identity,
                    "layers": model_spec.layers, "hidden": model_spec.hidden,
                    "prompt_ids": list(model_spec.frozen_prompt_ids)}
        for key, expected in identity.items():
            if oracle.get(key) != expected:
                raise ValueError(f"family oracle {key}: expected {expected}")
        if len(oracle.get("tokens", [])) < generation_steps or not isinstance(oracle.get("first_token"), dict):
            raise ValueError("family oracle lacks the requested token reference")
    elif generation_steps == 16:
        capture = _load(ROOT / "TinyStories/capture_fixed_point_continuation_oracle.py", "continuation_oracle_for_calyx")
        continuation = json.loads((ROOT / "artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json").read_text())
        capture.verify_continuation_oracle(continuation)
        oracle = json.loads(Path(oracle_path).read_text())
        if oracle.get("artifact_sha256") != continuation["identity"]["legacy_artifact_sha256"]:
            raise ValueError("sixteen-step oracle must retain the authenticated two-step model identity")
    else:
        capture = _load(ROOT / "TinyStories/capture_fixed_point_model_token_step_oracle.py", "model_oracle_for_calyx")
        oracle = json.loads(Path(oracle_path).read_text())
        capture.verify_oracle_fixture(oracle)
    hidden = model_spec.hidden if model_spec is not None else 64
    layers = model_spec.layers if model_spec is not None else 8
    vocab = model_spec.vocab if model_spec is not None else 50257
    token_plan = build_token_code_plan(vocab=vocab, hidden=hidden,
                                       bank_depth=TOKEN_CODE_BANK_DEPTH)
    token_bank_count = token_plan.bank_count
    token_scale_bank_count = (vocab + TOKEN_SCALE_BANK_DEPTH - 1) // TOKEN_SCALE_BANK_DEPTH
    block, attention, mlp = _dependencies()
    base = block._block_kernel_futil()
    base_cells, _, _ = mlp._component_sections(base)
    source_names = set(block._memory_contracts(attention, mlp)[0]) - {"block_input_q16_16"}
    shared_luts = {"attention_exp_lut_q1_20", "gelu_lut_q12"}
    sources = {}
    memory_cells = []
    banks = {}
    for match in re.finditer(r"(?:@external )?(\w+) = seq_mem_d1\((\d+), (\d+), (\d+)\);", base_cells):
        name, width, count, address = match[1], int(match[2]), int(match[3]), int(match[4])
        count = _family_block_memory_count(name, count, sources=source_names,
                                           rows=geometry.row_capacity, hidden=hidden)
        if name in source_names:
            if name in shared_luts:
                source = {"kind": "lut", "buffer": "_exp_lut" if name.startswith("attention") else "_gelu_lut"}
            else:
                kind, binding = _source_binding(name)
                source = {"kind": kind, "buffer": binding, "banks": layers, "bank_elements": count}
                banks[name] = count
                count *= layers
            sources[name] = {**source, "width": width, "elements": count}
        memory_cells.append(_memory(name, width, count))
    new_sources = {
        "prompt_tokens": {"kind": "prompt", "width": 16, "elements": 4},
        "position_weight_codes_i8": {"kind": "code", "buffer": "position_embedding.weight", "width": 8, "elements": 32 * hidden},
        "position_weight_scale_q8_24": {"kind": "scale", "buffer": "position_embedding.weight", "width": 64, "elements": 32},
        "final_gamma_q16_16": {"kind": "parameter", "buffer": "final_ln.weight", "width": 64, "elements": hidden},
        "final_beta_q16_16": {"kind": "parameter", "buffer": "final_ln.bias", "width": 64, "elements": hidden},
        "lm_input_scale_q8_24": {"kind": "scale", "buffer": "activation::lm_head.input", "width": 64, "elements": hidden},
    }
    if production:
        for bank in range(token_bank_count):
            new_sources[f"token_weight_codes_packed_b{bank}"] = {
                "kind": "code", "buffer": "token_embedding.weight", "width": 64,
                "elements": TOKEN_CODE_BANK_DEPTH,
                "banked": {"source_elements": vocab * hidden, "bank": bank, "banks": token_bank_count, "depth": TOKEN_CODE_BANK_DEPTH, "layout": "token_rows_2048_then_hidden_packed_i8" if model_spec is None else "token_rows_power2_then_hidden_packed_i8", "lanes": PACKED_TOKEN_CODE_LANES,
                           **({"rows_per_bank": token_plan.rows_per_bank, "hidden": hidden} if model_spec is not None else {})},
            }
        for bank in range(token_scale_bank_count):
            new_sources[f"token_weight_scale_q8_24_b{bank}"] = {
                "kind": "scale", "buffer": "token_embedding.weight", "width": 32,
                "elements": TOKEN_SCALE_BANK_DEPTH,
                "banked": {"source_elements": vocab, "bank": bank, "banks": token_scale_bank_count, "depth": TOKEN_SCALE_BANK_DEPTH, "layout": "contiguous_token_rows"},
            }
    else:
        old_sources = {
            "token_weight_codes_i8": {"kind": "code", "buffer": "token_embedding.weight", "width": 8, "elements": vocab * hidden},
            "token_weight_scale_q8_24": {"kind": "scale", "buffer": "token_embedding.weight", "width": 64, "elements": vocab},
        }
        new_sources = {"prompt_tokens": new_sources.pop("prompt_tokens"), **old_sources, **new_sources}
    sources.update(new_sources)
    memory_cells += [_memory(name, record["width"], record["elements"]) for name, record in new_sources.items()]
    if production and audit_checkpoints:
        # Explicit diagnostic builds retain the same production arithmetic and
        # schedule while exposing bounded semantic checkpoints.  They are not
        # the BRAM production image and are used only for localization.
        hardware = {
            "context_tokens": (16, geometry.row_capacity),
            "selected_tokens": (16, geometry.output_slots),
            "model_embedding": (64, generation_steps * geometry.row_capacity * hidden),
            "model_token_embedding": (64, generation_steps * geometry.row_capacity * hidden),
            "model_position_embedding": (64, generation_steps * geometry.row_capacity * hidden),
            "model_block_outputs": (64, generation_steps * geometry.row_capacity * hidden * layers),
            "model_final_ln": (64, generation_steps * geometry.row_capacity * hidden),
            "final_output_q16_16": (64, geometry.row_capacity * hidden),
            "lm_input_codes_i8": (8, geometry.row_capacity * hidden),
            "lm_input_q16_16": (64, geometry.row_capacity * hidden),
            # The production LM head streams one vocabulary row at a time;
            # retain only that accumulator in an audit build as well.
            "lm_accumulator_i64": (64, vocab),
        }
    elif production:
        # The streamed LM head reuses one vocabulary-row accumulator for each
        # real context row; it never materializes rows*vocabulary results.
        hardware = {"context_tokens": (16, geometry.row_capacity), "selected_tokens": (16, geometry.output_slots), "final_output_q16_16": (64, geometry.row_capacity * hidden), "lm_input_codes_i8": (8, geometry.row_capacity * hidden), "lm_input_q16_16": (64, geometry.row_capacity * hidden)}
    else:
        hardware = {"context_tokens": (16, 8), "selected_tokens": (16, 2), "model_embedding": (64, 1024), "model_token_embedding": (64, 1024), "model_position_embedding": (64, 1024), "model_block_outputs": (64, 8192), "model_final_ln": (64, 1024), "final_output_q16_16": (64, 512), "lm_input_codes_i8": (8, 512), "lm_input_q16_16": (64, 512), "lm_accumulator_i64": (64, 524288), "model_logits": (64, 1048576)}
    memory_cells += [_memory(name, *descriptor) for name, descriptor in hardware.items()]
    # The frozen 1M reachable certificate authorizes its 53-bit path only.
    # Family packages get the full-width post-scale path independently of
    # safe INT8/Q24 term narrowing and memory compaction.
    phases = _block_phases(block, attention, mlp, narrow_products=production,
                           requantization_width=64 if model_spec is not None else None,
                           rows=geometry.row_capacity, hidden=hidden)
    b = Builder()
    b.cells += memory_cells
    block_wires = "\n".join(wire for _, wires, _ in phases for wire in wires)
    b.cells += [cell for cells, _, _ in phases for cell in cells]
    for name, local_words in banks.items():
        bank_cells, bank_wires, bank_address = _layer_bank_address(
            name, local_words, layers, "model_layer_bank.out", "__LOCAL_INDEX__"
        )
        b.cells += bank_cells
        # Keep both concatenation inputs in the memory-access group. Mixing
        # a continuous bank input with a group-local index makes Calyx's
        # dead-assign-removal drop the upstream group-local index drivers.
        block_wires, count = re.subn(
            rf"\b{name}\.addr0 = ([^;]+);",
            lambda match: " ".join(wire.replace("__LOCAL_INDEX__", match[1])
                                   for wire in bank_wires)
                          + f" {name}.addr0 = {bank_address};",
            block_wires,
        )
        if count == 0:
            raise ValueError(f"unused model parameter bank: {name}")
    b.wires.append(block_wires)
    final_phase = _layer_norm(attention, final=True, rows=geometry.row_capacity,
                              hidden=hidden)
    qdq_phase = mlp._activation_qdq_phase("lm_input_qdq", "final_output_q16_16", "lm_input_scale_q8_24", "lm_input_codes_i8", "lm_input_q16_16", geometry.row_capacity * hidden, hidden, narrow_products=production)
    gemv_phase = mlp._gemv_phase(
        "lm_gemv", "lm_input_codes_i8", "lm_input_scale_q8_24",
        "token_weight_codes_packed" if production else "token_weight_codes_i8", "lm_accumulator_i64",
        1 if production else 8, hidden, vocab,
        input_row_expr="model_last_row.out" if production else None,
        input_row_width=geometry.context_length_width if production else None,
        input_address_width_override=geometry.row_address_width + (hidden - 1).bit_length() if production else None,
        write_trace=not production,
        banked_weights=("token_weight_codes_packed", token_bank_count, 14) if production else None,
        narrow_products=production,
        packed_weight=production,
    )
    # Restrict expensive vocabulary projections to real context rows.
    gemv_phase = (gemv_phase[0], [w.replace("lm_gemv_row_lt.right = 4'd8;", "lm_gemv_row_lt.right = model_context_length.out;") for w in gemv_phase[1]], gemv_phase[2])
    for cells, wires, _ in (final_phase, qdq_phase, gemv_phase):
        b.cells += cells
        b.wires += wires
    control = _orchestration(
        b,
        "\n".join(p[2] for p in phases),
        final_phase[2],
        qdq_phase[2] if production else qdq_phase[2] + gemv_phase[2],
        audit_checkpoints=audit_checkpoints,
        stream_lm_head=production,
        stream_gemv_control=gemv_phase[2] if production else None,
        board_io=production,
        generation_steps=generation_steps,
        layers=layers,
        hidden=hidden,
        vocab=vocab,
    )
    futil = '''// Compiler-owned exact model orchestration; no imported hand-written RTL.
import "primitives/core.futil";
import "primitives/binary_operators.futil";
import "primitives/memories/seq.futil";
''' + (f"component main(@go start: 1, valid: 1, token_index: {geometry.token_index_width}, prompt_0: 16, prompt_1: 16, prompt_2: 16, prompt_3: 16) -> (@done done: 1, token_id: 16) {{" if production else "component main(@go start: 1, valid: 1) -> (@done done: 1) {") + '''
  cells {
''' + mlp._indent_lines(b.cells, 4) + "\n  }\n  wires {\n" + mlp._indent_lines(b.wires, 4) + "\n  }\n  control {\n" + control + "\n  }\n}\n"
    from scripts.pipeline.calyx_program import Program, Reference, Expression
    program = Program.parse(futil)
    if production:
        # Authenticated scale/parameter tensors have been range-checked to
        # signed 32 bits. Store them narrowly and sign-extend at the consumer;
        # this halves the physical width without changing arithmetic values.
        narrow_names = [
            name for name, record in sources.items()
            if record.get("width") == 64 and record.get("kind") in {"scale", "parameter"}
        ]
        program = narrow_source_memory_words(program, narrow_names)
        for name in narrow_names:
            sources[name]["width"] = 32
        # The authenticated GELU table is bounded to signed 16 bits.  Keep
        # its lookup semantics wide at the consumer while using the native
        # 18-bit BRAM lane, halving its bank count.
        program = narrow_source_memory_words(program, ["gelu_lut_q12"], target_width=16)
        sources["gelu_lut_q12"]["width"] = 16
        # These accumulators execute serially on the shared GEMV datapath and
        # are never live at the same time. Keep the alias set explicit rather
        # than attempting heuristic liveness inference.
        program = alias_runtime_memories(
            program,
            {
                "k_accumulator_i64": "q_accumulator_i64",
                "v_accumulator_i64": "q_accumulator_i64",
                "out_accumulator_i64": "q_accumulator_i64",
                "c_proj_accumulator_i64": "q_accumulator_i64",
                # Every listed INT8 code buffer is a 1,280-word transient:
                # its producer/consumer completes before the next projection
                # starts, and no later phase retains the codes.
                "k_input_codes_i8": "q_input_codes_i8",
                "v_input_codes_i8": "q_input_codes_i8",
                "out_input_codes_i8": "q_input_codes_i8",
                "q_output_codes_i8": "q_input_codes_i8",
                "k_output_codes_i8": "q_input_codes_i8",
                "v_output_codes_i8": "q_input_codes_i8",
                "out_output_codes_i8": "q_input_codes_i8",
                # Keep the LM-head input codes separate.  The LM Q/DQ phase
                # consumes final_output_q16_16 after the last transformer
                # block; unlike the serial projection buffers above, it is
                # live across the LM-head handoff.  Reusing q_input_codes_i8
                # changes the observed LM boundary before the first token.
                "c_proj_output_codes_i8": "c_fc_input_codes_i8",
                "c_proj_input_codes_i8": "c_fc_output_codes_i8",
                # GELU has consumed c_fc_output_q16_16 before the c_proj
                # input Q/DQ phase writes c_proj_input_q16_16.  Both are
                # 64-bit, rows*256-word transient buffers, so they can share
                # storage without changing the phase ordering or ABI.
                "c_proj_input_q16_16": "c_fc_output_q16_16",
                # The c_fc accumulator is consumed by its requantization
                # phase before the c_fc output buffer is read by GELU.  Both
                # are serial 64-bit buffers with the same extent.
                "c_fc_accumulator_i64": "c_fc_output_q16_16",
                # The post-weight-rescale c_fc values are consumed before
                # the output Q/DQ phase writes the same serial buffer.
                "c_fc_post_weight_rescale_bias_q16_16": "c_fc_output_q16_16",
                # GELU is an elementwise serial transform: each c_fc output
                # word is read before the corresponding result is written.
                "gelu_output_q16_16": "c_fc_output_q16_16",
                # The attention numerator is consumed before the corresponding
                # context row is written, so the serial 64-bit buffers can
                # share storage.
                "attention_numerator_q17_36": "attention_context_q16_16",
                # The context row has been consumed by the output projection
                # before the residual row is written.
                "attention_residual_q16_16": "attention_context_q16_16",
                "q_post_weight_rescale_bias_q16_16": "attention_context_q16_16",
                "k_post_weight_rescale_bias_q16_16": "attention_context_q16_16",
                "v_post_weight_rescale_bias_q16_16": "attention_context_q16_16",
                "out_post_weight_rescale_bias_q16_16": "attention_context_q16_16",
                "attention_denominator_q1_20": "attention_maximum_q8",
                # Final layer-normalization output is consumed before the
                # next generation step reuses block input storage.
                "final_output_q16_16": "block_input_q16_16",
                # The projection result is consumed by the residual write
                # before the next layer's layer-normalization output is live.
                "c_proj_output_q16_16": "ln1_output_q16_16",
                # Attention context-head storage is consumed before the next
                # layer-normalization result is produced.
                "attention_context_heads_q16_16": "ln1_output_q16_16",
            },
        )
        # These score, delta, and probability values are range-bounded fixed-
        # point intermediates. Store them in 32-bit words while preserving the
        # existing 64-bit arithmetic interfaces through explicit adapters.
        program = narrow_runtime_memory_words(
            program,
            ["attention_score_q8", "attention_delta_q8", "attention_exp_q1_20"],
        )
    if generation_steps == 16:
        # The physical storage is provisioned for the largest context, but a
        # step must execute only its real rows.  Running padded future rows
        # changes shared intermediate memories and was observed to change the
        # first selected token (1544 versus the authenticated 11).  Keep the
        # capacity static while binding every row loop to the live context.
        for assignment in program.assignments():
            target = assignment.target
            if target.port == "right" and (target.cell == "row_lt" or target.cell.endswith("_row_lt")) and assignment.value.parts == ["5'd20"]:
                assignment.value = Expression([Reference("model_context_length", "out")])
    from scripts.pipeline.calyx_validation import validate_program
    validate_program(program)
    futil = program.serialize()
    # Publish the physical bank geometry alongside the logical Calyx contract.
    # This is deliberately metadata-only for now: the upcoming banked-memory
    # lowering consumes it, while simulation continues to use the logical
    # ``seq_mem_d1`` ABI unchanged.
    runtime_bram_plan = {}
    for cell in program.cells.values():
        if cell.primitive == "seq_mem_d1":
            width, count, _ = cell.parameters
            if count >= 512:
                runtime_bram_plan[cell.name] = bram_bank_plan(width, count)
    provenance = {
        "schema": SCHEMA if model_spec is None else "task5-tinystories-family-model-calyx-v1",
        "oracle_sha256": oracle["artifact_sha256"] if model_spec is None else _canonical(oracle),
        "futil_sha256": _sha(futil.encode()), "base_block_futil_sha256": _sha(base.encode()),
        "compiler_sources": {str(Path(module.__file__).relative_to(ROOT)): _sha(Path(module.__file__).read_bytes()) for module in (block, attention, mlp)},
        "orchestrator_sha256": _sha(Path(__file__).read_bytes()),
        "schedule": {"layers": list(range(layers)), **({"hidden": hidden} if model_spec is not None else {}), "shared_block_datapaths": 1, "row_capacity": geometry.row_capacity, "real_context_lengths": list(geometry.real_context_lengths), "padded_future_rows": "causally_disconnected_from_real_rows", "lm_head_rows": "real_context_only", "vocabulary": vocab, "vocabulary_storage_stride": 65536},
        "source_memories": sources,
        "hardware_owned_memories": sorted({cell.name for cell in program.cells.values() if cell.primitive == "seq_mem_d1"} - set(sources)),
        "feedback": "greedy_last_real_row -> context_tokens[context_length] -> token_embedding",
        "claims": {"compiler_owned_control": True, "existing_arithmetic_generators_modified": production, "simulation_checkpoints": bool(audit_checkpoints), "sv_execution_verified": False, "board_executed": False, "board_fit_verified": False},
        "production_memory_contract": {"audit_checkpoint_memories": [] if production else ["model_embedding", "model_token_embedding", "model_position_embedding", "model_block_outputs", "model_final_ln", "model_logits"], "full_logits_storage": not production, "streamed_argmax": True},
        "runtime_bram_bank_plan": runtime_bram_plan,
        "projection_weight_bank_plan": projection_weight_bank_plan(),
        "projection_weight_packing_plan": projection_weight_packing_plan(),
    }
    provenance["schedule"]["generation_steps"] = generation_steps
    provenance["schedule"]["real_context_lengths"] = list(geometry.real_context_lengths)
    provenance["artifact_sha256"] = _canonical(provenance)
    return CalyxArtifact(futil, provenance)


def compile_model_kernel(artifact: CalyxArtifact, directory: Path, *, timeout: int = 1800) -> dict[str, Any]:
    if not 0 < timeout <= 7200:
        raise ValueError("compilation timeout must be in (0, 7200]")
    if _sha(artifact.futil.encode()) != artifact.provenance["futil_sha256"]:
        raise ValueError("generated Futil identity mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + timeout
    futil = directory / "model.futil"
    verilog = directory / "model.sv"
    futil.write_text(artifact.futil)
    def run_stage(command, stage):
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=remaining)
        except subprocess.TimeoutExpired as error:
            failure = {"status": "deadline_exceeded", "stage": stage, "timeout_seconds": timeout, "elapsed_seconds": time.monotonic() - started, "futil_sha256": artifact.provenance["futil_sha256"], "execution_verified": False}
            (directory / "compile.json").write_text(json.dumps(failure, indent=2) + "\n")
            raise ValueError(f"deadline_exceeded: {stage}") from error
        (directory / f"{stage}.log").write_text(run.stderr)
        if run.returncode:
            failure = {"status": "compilation_failed", "stage": stage, "returncode": run.returncode, "diagnostic": run.stderr[-6000:], "futil_sha256": artifact.provenance["futil_sha256"], "execution_verified": False}
            (directory / "compile.json").write_text(json.dumps(failure, indent=2) + "\n")
            raise ValueError(f"model Calyx lowering failed ({run.returncode}): {run.stderr[-6000:]}")
        return run
    tool = run_stage(["nix", "build", "--no-link", "--print-out-paths", ".#calyx"], "calyx_tool_resolution")
    paths = tool.stdout.strip().splitlines()
    if len(paths) != 1:
        raise ValueError("Calyx tool resolution did not yield one store path")
    install = Path(paths[0])
    command = [str(install / "bin/calyx"), str(futil), "-l", str(install / "share/calyx"), "-d", "cell-share", "-b", "verilog"]
    run = run_stage(command, "calyx_lowering")
    verilog.write_text(run.stdout)
    top = re.search(r"module main\s*\((.*?)\);", run.stdout, re.S)
    if not top:
        raise ValueError("generated SV lacks main")
    ports = [name for name in ("clk", "reset", "start", "valid", "done") if re.search(rf"\b{name}\b", top[1])]
    receipt = {"status": "generated_sv", "futil_sha256": artifact.provenance["futil_sha256"], "verilog_sha256": _sha(run.stdout.encode()), "verilog_bytes": len(run.stdout.encode()), "top_ports": ports, "elapsed_seconds": time.monotonic() - started, "timeout_seconds": timeout, "command": command, "execution_verified": False}
    (directory / "compile.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def materialize_model_sources(artifact: CalyxArtifact, bundle) -> dict[str, Any]:
    """Only immutable authenticated model inputs; never oracle intermediates."""
    import torch
    sources = {}
    for name, record in artifact.provenance["source_memories"].items():
        if record["kind"] == "prompt":
            value = torch.tensor([7454, 2402, 257, 640], dtype=torch.int64)
        elif record["kind"] == "lut":
            value = getattr(bundle.model, record["buffer"])
        elif record.get("banks"):
            value = torch.cat([bundle.model._buffer(record["kind"], record["buffer"].format(layer=layer)).reshape(-1) for layer in range(record["banks"])])
        else:
            value = bundle.model._buffer(record["kind"], record["buffer"]).reshape(-1)
        if record.get("banked"):
            layout = record["banked"]["layout"]
            bank = int(record["banked"]["bank"])
            depth = int(record["banked"]["depth"])
            if layout in {"token_rows_2048_then_hidden_packed_i8",
                          "token_rows_power2_then_hidden_packed_i8"}:
                hidden = int(record["banked"].get("hidden", 64))
                source_elements = int(record["banked"]["source_elements"])
                if source_elements % hidden:
                    raise ValueError(f"{name}: token-code logical shape is not row-aligned")
                plan = build_token_code_plan(vocab=source_elements // hidden,
                                             hidden=hidden, bank_depth=depth)
                if (plan.bank_count != record["banked"]["banks"]
                    or ("rows_per_bank" in record["banked"]
                        and plan.rows_per_bank != record["banked"]["rows_per_bank"])):
                    raise ValueError(f"{name}: token-code physical plan differs from source ABI")
                packed_words = materialize_token_code_bank(
                    plan, value.detach().cpu().numpy(), bank
                )
                # Source memories are serialized as signed int64 words, while
                # packing is defined over the complete 64-bit bit pattern.
                # Convert high-bit words to their two's-complement int64
                # representation before constructing the torch tensor.
                value = torch.tensor(
                    [word - (1 << 64) if word >= (1 << 63) else word for word in packed_words],
                    dtype=torch.int64,
                )
            elif layout in {"token_rows_256_then_hidden", "token_rows_512_then_hidden"}:
                row_stride = 64
                rows_per_bank = depth // row_stride
                start = bank * rows_per_bank * row_stride
                value = value[start:start + depth]
            elif layout == "contiguous_token_rows":
                start = bank * depth
                value = value[start:start + depth]
            else:
                raise ValueError(f"unknown banked source layout: {layout}")
            if value.numel() < depth:
                import torch
                value = torch.cat([value, torch.zeros(depth - value.numel(), dtype=value.dtype)])
        value = value.detach().cpu().to(torch.int64).flatten().contiguous()
        if value.numel() != record["elements"]:
            raise ValueError(f"source memory shape mismatch: {name}")
        sources[name] = value
    return sources
