#!/usr/bin/env python3
# Copyright (c) 2026 Kevin Baragona
# SPDX-License-Identifier: Apache-2.0
"""Translate identified Yosys JSON into Loom's neutral import JSON.

Yosys and this adapter are untrusted.  The emitted tree is consumed by Loom's
fail-closed `ImportIR` validator/lowering and must still pass original-vs-Loom
RTL equivalence.  Unsupported cells are retained as source-located blockers;
they are never guessed or dropped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any


SEQUENTIAL = {"$dff", "$dffe", "$sdff", "$sdffe", "$sdffce", "$adff", "$adffe"}
SUPPORTED_COMB = {
    "$not": "bit_not", "$neg": "negate",
    "$and": "bit_and", "$or": "bit_or", "$xor": "bit_xor",
    "$add": "add", "$sub": "sub", "$mul": "mul", "$div": "unsigned_div",
    "$mod": "unsigned_rem", "$shl": "shift_left", "$shr": "logical_shift_right",
    "$shift": "normalized_signed_direction_shift",
    "$sshr": "normalized_arithmetic_shift_right",
    "$eq": "equal", "$lt": "less_than", "$mux": "mux",
    "$logic_and": "logical_and", "$logic_or": "logical_or",
    "$logic_not": "logical_not", "$reduce_and": "reduce_and",
    "$reduce_or": "reduce_bool", "$reduce_bool": "reduce_bool",
    "$ne": "not_equal", "$ge": "greater_equal", "$gt": "greater_than",
    "$le": "less_equal",
    "$shiftx": "explicit_partial_variable_part_select",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_parameter(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return default
    clean = value.replace("x", "0").replace("z", "0")
    try:
        return int(clean, 2)
    except ValueError:
        try:
            return int(value, 0)
        except ValueError:
            return default


LOCATION = re.compile(r"^(.*?):(\d+)\.(\d+)-(\d+)\.(\d+)$")


def location(src: str, fallback: str = "<yosys>") -> dict[str, Any]:
    first = src.split("|")[0].strip() if src else ""
    match = LOCATION.match(first)
    if not match:
        return {"file": fallback, "start_line": 1, "start_column": 0,
                "end_line": 1, "end_column": 0}
    start_line = int(match.group(2))
    end_line = int(match.group(4))
    # Yosys uses 0.0-0.0 for generated netlist objects. Preserve the file but
    # map that sentinel to a valid synthetic source position; site hashes still
    # distinguish generated partial values by module and bit pattern.
    if start_line == 0 and end_line == 0:
        start_line = end_line = 1
    return {"file": match.group(1), "start_line": start_line,
            "start_column": int(match.group(3)), "end_line": end_line,
            "end_column": int(match.group(5))}


def literal(width: int, value: int, src: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "literal", "width": width, "value": value, "source": src}


def partial_pattern(bits: list[str]) -> str:
    """Render a bounded diagnostic without dumping hundreds of identical bits."""
    msb_first = "".join(reversed(bits))
    if len(set(bits)) == 1:
        return f"{len(bits)}'{bits[0]}"
    if len(bits) <= 64:
        return f"{len(bits)}'{msb_first}"
    unknown = sum(bit not in ("0", "1") for bit in bits)
    return f"width={len(bits)} unknown={unknown} pattern_sha256={sha256(msb_first.encode())[:16]}"


def signal(width: int, name: str, src: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "signal", "width": width, "name": name, "source": src}


def slice_expr(value: dict[str, Any], offset: int, width: int,
               src: dict[str, Any]) -> dict[str, Any]:
    if offset == 0 and value["width"] == width:
        return value
    return {"kind": "slice", "width": width, "value": value,
            "offset": offset, "source": src}


def resize(value: dict[str, Any], width: int, signed: bool,
           src: dict[str, Any]) -> dict[str, Any]:
    if value["width"] == width:
        return value
    if value["width"] < width:
        return {"kind": "sign_extend" if signed else "zero_extend",
                "width": width, "value": value, "source": src}
    return slice_expr(value, 0, width, src)


def unary(width: int, op: str, value: dict[str, Any],
          src: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "unary", "width": width, "op": op,
            "value": value, "source": src}


def binary(width: int, op: str, left: dict[str, Any], right: dict[str, Any],
           src: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "binary", "width": width, "op": op,
            "left": left, "right": right, "source": src}


def booleanize(value: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    return unary(1, "reduce_bool", value, src)


def shifted_operand(value: dict[str, Any], amount: dict[str, Any], width: int,
                    signed_value: bool, src: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Put a shift value and amount in one width without losing amount bits."""
    work_width = max(value["width"], amount["width"], width)
    return (resize(value, work_width, signed_value, src),
            resize(amount, work_width, False, src))


def arithmetic_shift_right(value: dict[str, Any], amount: dict[str, Any],
                           width: int, signed_value: bool,
                           src: dict[str, Any]) -> dict[str, Any]:
    base_width = max(value["width"], width)
    # A logical shift over a sign extension twice as wide preserves the
    # shifted-in sign bits for every in-range amount. The explicit out-of-
    # range branch below handles amounts at least the result's base width.
    work_width = max(base_width * 2 if signed_value else base_width,
                     amount["width"])
    value = resize(value, work_width, signed_value, src)
    amount = resize(amount, work_width, False, src)
    shifted = binary(value["width"], "logical_shift_right", value, amount, src)
    if signed_value:
        sign = slice_expr(value, value["width"] - 1, 1, src)
        fill = {"kind": "mux", "width": value["width"], "condition": sign,
                "yes": literal(value["width"], (1 << value["width"]) - 1, src),
                "no": literal(value["width"], 0, src), "source": src}
        in_range = binary(1, "unsigned_less_than", amount,
                          literal(value["width"], base_width, src), src)
        shifted = {"kind": "mux", "width": value["width"],
                   "condition": in_range, "yes": shifted, "no": fill,
                   "source": src}
    return resize(shifted, width, signed_value, src)


@dataclass
class Driver:
    cell_name: str
    port: str
    offset: int


@dataclass
class MemoryInfo:
    cell_name: str
    original_name: str
    source: dict[str, Any]
    size: int
    width: int
    abits: int
    address_width: int
    read_ports: int
    write_ports: int
    connections: dict[str, list[Any]]
    init_bits: list[str]
    init_expr: dict[str, Any]
    loom_name: str


class FourStatePolicy:
    CLASSIFICATIONS = {
        "synthesis_dont_care", "unreachable_decode", "undriven_behavior",
        "uninitialized_state_or_memory",
    }

    def __init__(self, document: dict[str, Any]):
        if document.get("schema") != 1:
            raise ValueError("unsupported four-state policy schema")
        self.rules = document.get("rules")
        if not isinstance(self.rules, list) or not self.rules:
            raise ValueError("four-state policy must contain at least one rule")
        names = []
        for rule in self.rules:
            required = {"name", "module", "file", "line_start", "line_end",
                        "classification", "fill", "rationale"}
            if not isinstance(rule, dict) or not required.issubset(rule):
                raise ValueError("four-state policy rule is incomplete")
            if rule["classification"] not in self.CLASSIFICATIONS:
                raise ValueError(f"unknown four-state classification {rule['classification']!r}")
            if rule["fill"] not in ("zero", "one"):
                raise ValueError("four-state fill must be 'zero' or 'one'")
            if (not rule["name"] or not rule["rationale"] or
                    not isinstance(rule["line_start"], int) or
                    not isinstance(rule["line_end"], int) or
                    rule["line_start"] < 0 or rule["line_end"] < rule["line_start"]):
                raise ValueError("four-state policy rule has invalid metadata")
            if "site" in rule and (not isinstance(rule["site"], str) or
                                   not rule["site"].startswith("four_state_")):
                raise ValueError("four-state policy site must be a stable four_state_ identifier")
            names.append(rule["name"])
        if len(names) != len(set(names)):
            raise ValueError("four-state policy rule names must be unique")

    def matching(self, site: str, module: str,
                 source: dict[str, Any]) -> list[dict[str, Any]]:
        line = source["start_line"]
        return [rule for rule in self.rules
                if rule.get("site", site) == site
                and rule["module"] in ("*", module)
                and rule["file"] in ("*", source["file"])
                and rule["line_start"] <= line <= rule["line_end"]]


class ModuleTranslator:
    def __init__(self, name: str, module: dict[str, Any], modules: dict[str, Any],
                 four_state_policy: FourStatePolicy | None = None):
        self.name = name
        self.module = module
        self.modules = modules
        self.module_source = location(module.get("attributes", {}).get("src", ""))
        self.netnames = module.get("netnames", {})
        self.cells = module.get("cells", {})
        self.bit_names: dict[int, list[tuple[str, int, int]]] = {}
        self.inputs: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
        self.registers: dict[tuple[Any, ...], str] = {}
        self.register_originals: dict[tuple[Any, ...], tuple[str, int]] = {}
        self.drivers: dict[int, Driver] = {}
        self.expression_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.in_progress: set[tuple[str, str]] = set()
        self.memory_cells: dict[str, MemoryInfo] = {}
        self.register_equivalence: list[dict[str, Any]] = []
        self.unsupported: list[dict[str, Any]] = []
        self.four_state_policy = four_state_policy
        self._index()

    def _index(self) -> None:
        for name, net in self.netnames.items():
            bits = net.get("bits", [])
            for offset, bit in enumerate(bits):
                if isinstance(bit, int):
                    self.bit_names.setdefault(bit, []).append((name, offset, len(bits)))
        for name, port in self.module.get("ports", {}).items():
            if port.get("direction") == "input":
                src = self.source_for_bits(port.get("bits", []))
                self.inputs[tuple(port.get("bits", []))] = (name, src)
        for cell_name, cell in self.cells.items():
            for port, direction in cell.get("port_directions", {}).items():
                if direction != "output":
                    continue
                for offset, bit in enumerate(cell.get("connections", {}).get(port, [])):
                    if isinstance(bit, int):
                        if bit in self.drivers:
                            self.block("multiple_drivers", f"bit {bit} has multiple cell drivers",
                                       cell.get("attributes", {}).get("src", ""))
                        self.drivers[bit] = Driver(cell_name, port, offset)

    def source_for_bits(self, bits: list[Any]) -> dict[str, Any]:
        target = tuple(bits)
        choices = []
        for _, net in self.netnames.items():
            if tuple(net.get("bits", [])) == target:
                choices.append(net.get("attributes", {}).get("src", ""))
        return location(next((item for item in choices if item), ""),
                        self.module_source["file"])

    def public_name(self, bits: list[Any], fallback: str) -> str:
        target = tuple(bits)
        names = [name for name, net in self.netnames.items()
                 if tuple(net.get("bits", [])) == target and not name.startswith("$")]
        return sorted(names, key=lambda item: (len(item), item))[0] if names else fallback

    def register_original(self, bits: list[Any], fallback: str) -> tuple[str, int]:
        """Return a stable public net and LSB offset for a register Q vector.

        Yosys sometimes splits one source register into several `$dff` cells.
        Those generated cell names are not nets and disappear in later passes,
        while the Q vectors remain contiguous slices of the source-level net.
        Recording that containing net makes the equivalence correspondence
        independent of Yosys' generated cell naming.
        """
        target = tuple(bits)
        choices: list[tuple[int, str, int]] = []
        for name, net in self.netnames.items():
            if name.startswith("$"):
                continue
            candidate = net.get("bits", [])
            for offset in range(len(candidate) - len(bits) + 1):
                if tuple(candidate[offset:offset + len(bits)]) == target:
                    choices.append((len(candidate), name, offset))
        if not choices:
            return fallback, 0
        _, name, offset = sorted(choices)[0]
        return name, offset

    def input_port_name(self, bits: list[Any], fallback: str) -> str:
        """Name the module boundary that physically supplies an input bit-vector.

        Yosys retains internal aliases for port bits. A clock domain must name
        the actual input port, not merely the shortest public net alias, or the
        checked Loom body would request a clock that its wrapper cannot bind.
        """
        target = tuple(bits)
        names = [name for name, port in self.module.get("ports", {}).items()
                 if port.get("direction") == "input" and
                 tuple(port.get("bits", [])) == target]
        if names:
            return sorted(names, key=lambda item: (len(item), item))[0]
        return self.public_name(bits, fallback)

    @staticmethod
    def legal_identifier(name: str) -> str:
        """Injectively encode arbitrary UTF-8 Yosys names as HDL identifiers."""
        return "_h" + name.encode("utf-8").hex()

    def instance_net(self, cell_name: str, port: str) -> str:
        return ("__loom_child_" + self.legal_identifier(cell_name) +
                "__" + self.legal_identifier(port))

    def block(self, kind: str, detail: str, src: str | dict[str, Any],
              metadata: dict[str, Any] | None = None) -> None:
        source = src if isinstance(src, dict) else location(src, self.module_source["file"])
        record = {"kind": kind, "detail": detail, "source": source}
        if metadata is not None:
            record["metadata"] = metadata
        if record not in self.unsupported:
            self.unsupported.append(record)

    def constant(self, bits: list[Any], src: dict[str, Any]) -> dict[str, Any] | None:
        if not all(isinstance(bit, str) for bit in bits):
            return None
        if any(bit not in ("0", "1") for bit in bits):
            site_payload = {"module": self.name, "bits": bits, "source": src}
            site = "four_state_" + sha256(json.dumps(
                site_payload, sort_keys=True, separators=(",", ":")).encode())[:24]
            if self.four_state_policy is None:
                self.block("four_state_constant",
                           f"unclassified partial value {site} {partial_pattern(bits)}", src,
                           {"site": site, "pattern": partial_pattern(bits),
                            "width": len(bits),
                            "unknown_bits": sum(bit not in ("0", "1") for bit in bits)})
                return literal(len(bits), 0, src)
            matches = self.four_state_policy.matching(site, self.name, src)
            if len(matches) != 1:
                kind = ("four_state_policy_missing" if not matches else
                        "four_state_policy_ambiguous")
                self.block(kind,
                           f"partial value {site} matched {len(matches)} policy rules", src,
                           {"site": site, "pattern": partial_pattern(bits),
                            "width": len(bits),
                            "unknown_bits": sum(bit not in ("0", "1") for bit in bits)})
                return literal(len(bits), 0, src)
            rule = matches[0]
            known_mask = sum((1 << index) for index, bit in enumerate(bits)
                             if bit in ("0", "1"))
            known_value = sum((1 << index) for index, bit in enumerate(bits)
                              if bit == "1")
            unknown_mask = ((1 << len(bits)) - 1) ^ known_mask
            implementation_value = known_value
            if rule["fill"] == "one":
                implementation_value |= unknown_mask
            return {
                "kind": "partial_literal", "width": len(bits),
                "partial": {
                    "site": site,
                    "classification": rule["classification"],
                    "known_mask": known_mask,
                    "known_value": known_value,
                    "implementation_value": implementation_value,
                    "rationale": f"{rule['name']}: {rule['rationale']}",
                },
                "source": src,
            }
        value = sum((1 << index) for index, bit in enumerate(bits) if bit == "1")
        return literal(len(bits), value, src)

    @staticmethod
    def parameter_bits(value: Any, width: int) -> list[str] | None:
        """Decode a Yosys binary parameter into its LSB-first bit vector."""
        if isinstance(value, int):
            return ["1" if (value >> index) & 1 else "0" for index in range(width)]
        if not isinstance(value, str) or len(value) != width:
            return None
        return list(reversed(value.lower()))

    @staticmethod
    def packed_bit(value: Any, index: int, default: str = "0") -> Any:
        if isinstance(value, str):
            bits = list(reversed(value))
            return bits[index] if index < len(bits) else default
        if isinstance(value, int):
            return "1" if (value >> index) & 1 else "0"
        return default

    def partial_slice(self, expression: dict[str, Any], offset: int, width: int,
                      src: dict[str, Any]) -> dict[str, Any]:
        """Slice a literal while retaining a checked partial-value witness."""
        if expression["kind"] == "literal":
            return literal(width, (expression["value"] >> offset) & ((1 << width) - 1), src)
        choice = expression["partial"]
        mask = (1 << width) - 1
        return {
            "kind": "partial_literal", "width": width,
            "partial": {
                "site": choice["site"],
                "classification": choice["classification"],
                "known_mask": (choice["known_mask"] >> offset) & mask,
                "known_value": (choice["known_value"] >> offset) & mask,
                "implementation_value":
                    (choice["implementation_value"] >> offset) & mask,
                "rationale": choice["rationale"],
            },
            "source": src,
        }

    def partial_projection(self, expression: dict[str, Any], indexes: list[int],
                           src: dict[str, Any]) -> dict[str, Any] | None:
        """Project selected source bits into one packed memory initialization."""
        if expression["kind"] != "partial_literal":
            return None
        choice = expression["partial"]
        def project(field: str) -> int:
            return sum(((choice[field] >> source_index) & 1) << target_index
                       for target_index, source_index in enumerate(indexes))
        return {
            "site": choice["site"],
            "classification": choice["classification"],
            "known_mask": project("known_mask"),
            "known_value": project("known_value"),
            "implementation_value": project("implementation_value"),
            "rationale": choice["rationale"],
        }

    def vector_alias(self, bits: list[Any], aliases: dict[tuple[Any, ...], Any],
                     src: dict[str, Any]) -> dict[str, Any] | None:
        exact = aliases.get(tuple(bits))
        if exact is not None:
            name = exact[0] if isinstance(exact, tuple) else exact
            return signal(len(bits), name, src)
        if len(bits) != 1 or not isinstance(bits[0], int):
            return None
        bit = bits[0]
        for vector, entry in aliases.items():
            if bit in vector:
                name = entry[0] if isinstance(entry, tuple) else entry
                whole = signal(len(vector), name, src)
                return slice_expr(whole, vector.index(bit), 1, src)
        return None

    def expr(self, bits: list[Any], src: dict[str, Any] | None = None) -> dict[str, Any]:
        src = src or self.source_for_bits(bits)
        if not bits:
            self.block("zero_width", "empty signal vector", src)
            return literal(1, 0, src)
        result = self.constant(bits, src)
        if result is not None:
            return result
        result = self.vector_alias(bits, self.inputs, src)
        if result is not None:
            return result
        result = self.vector_alias(bits, self.registers, src)
        if result is not None:
            return result
        exact_driver = None
        for cell_name, cell in self.cells.items():
            for port, direction in cell.get("port_directions", {}).items():
                if direction == "output" and tuple(cell.get("connections", {}).get(port, [])) == tuple(bits):
                    exact_driver = (cell_name, port)
                    break
            if exact_driver:
                break
        if exact_driver:
            return self.cell_expr(*exact_driver)
        if len(bits) == 1 and isinstance(bits[0], int) and bits[0] in self.drivers:
            driver = self.drivers[bits[0]]
            whole = self.cell_expr(driver.cell_name, driver.port)
            return slice_expr(whole, driver.offset, 1, src)
        # Yosys vectors are LSB-first. Build the semantic vector MSB-to-LSB.
        parts = [self.expr([bit], src) for bit in reversed(bits)]
        value = parts[0]
        for part in parts[1:]:
            value = {"kind": "concat", "width": value["width"] + part["width"],
                     "high": value, "low": part, "source": src}
        return value

    def memory_address(self, info: MemoryInfo, port: int, write: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        connection = "WR_ADDR" if write else "RD_ADDR"
        packed = info.connections.get(connection, [])
        bits = packed[port * info.abits:(port + 1) * info.abits]
        full = self.expr(bits, info.source)
        address = slice_expr(full, 0, info.address_width, info.source)
        if info.abits == info.address_width:
            return address, literal(1, 1, info.source)
        in_range = binary(
            1, "unsigned_less_than", full,
            literal(info.abits, info.size, info.source), info.source)
        return address, in_range

    def memory_oob_fill(self, info: MemoryInfo) -> dict[str, Any]:
        site_payload = {"module": self.name, "cell": info.cell_name,
                        "kind": "$mem_v2_out_of_range_read", "source": info.source}
        site = "four_state_" + sha256(json.dumps(
            site_payload, sort_keys=True, separators=(",", ":")).encode())[:24]
        matches = ([] if self.four_state_policy is None else
                   self.four_state_policy.matching(site, self.name, info.source))
        if len(matches) != 1:
            kind = ("four_state_memory_out_of_range" if self.four_state_policy is None else
                    ("four_state_policy_missing" if not matches else
                     "four_state_policy_ambiguous"))
            self.block(kind, f"out-of-range memory read {site} matched {len(matches)} policy rules",
                       info.source, {"site": site, "pattern": f"{info.width}'dynamic-x",
                                     "width": info.width, "unknown_bits": info.width})
            return literal(info.width, 0, info.source)
        rule = matches[0]
        implementation = (1 << info.width) - 1 if rule["fill"] == "one" else 0
        return {"kind": "partial_literal", "width": info.width,
                "partial": {"site": site, "classification": rule["classification"],
                            "known_mask": 0, "known_value": 0,
                            "implementation_value": implementation,
                            "rationale": f"{rule['name']}: {rule['rationale']}"},
                "source": info.source}

    def memory_read(self, info: MemoryInfo, port: int) -> dict[str, Any]:
        address, in_range = self.memory_address(info, port, False)
        if info.write_ports == 0:
            words = [self.partial_slice(info.init_expr, index * info.width,
                                        info.width, info.source)
                     for index in range(info.size)]
            result = words[0]
            for index in range(1, info.size):
                selected = binary(1, "equal", address,
                                  literal(info.address_width, index, info.source), info.source)
                result = {"kind": "mux", "width": info.width,
                          "condition": selected, "yes": words[index], "no": result,
                          "source": info.source}
        else:
            result = {"kind": "memory_read", "width": info.width,
                      "memory": info.loom_name, "address": address,
                      "source": info.source}
        if info.abits != info.address_width:
            result = {"kind": "mux", "width": info.width,
                      "condition": in_range, "yes": result,
                      "no": self.memory_oob_fill(info), "source": info.source}
        return result

    def prepare_memories(self, clock_domains: set[tuple[tuple[Any, ...], bool]]) -> list[dict[str, Any]]:
        memories: list[dict[str, Any]] = []
        writable: list[MemoryInfo] = []
        for cell_name, cell in self.cells.items():
            kind = cell.get("type", "")
            if not kind.startswith("$mem"):
                continue
            src = location(cell.get("attributes", {}).get("src", ""),
                           self.module_source["file"])
            if kind != "$mem_v2":
                self.block("memory_cell", f"unsupported memory cell {kind}", src)
                continue
            params = cell.get("parameters", {})
            connections = cell.get("connections", {})
            size = decode_parameter(params.get("SIZE"))
            width = decode_parameter(params.get("WIDTH"))
            abits = decode_parameter(params.get("ABITS"))
            read_ports = decode_parameter(params.get("RD_PORTS"))
            write_ports = decode_parameter(params.get("WR_PORTS"))
            offset = decode_parameter(params.get("OFFSET"))
            if (size <= 0 or size & (size - 1) or width <= 0 or abits <= 0 or
                    read_ports <= 0 or offset != 0):
                self.block("memory_shape",
                           "$mem_v2 requires positive power-of-two SIZE, positive widths/read ports, and OFFSET=0",
                           src)
                continue
            address_width = (size - 1).bit_length()
            expected_lengths = {
                "RD_ADDR": read_ports * abits, "RD_DATA": read_ports * width,
                "WR_ADDR": write_ports * abits, "WR_DATA": write_ports * width,
                "WR_EN": write_ports * width, "WR_CLK": write_ports,
            }
            malformed = [name for name, expected in expected_lengths.items()
                         if len(connections.get(name, [])) != expected]
            if malformed:
                self.block("memory_port_shape",
                           "malformed packed $mem_v2 ports: " + ", ".join(malformed), src)
                continue
            unsupported_parameters = []
            for name in ("RD_CLK_ENABLE", "RD_TRANSPARENCY_MASK",
                         "RD_COLLISION_X_MASK", "RD_WIDE_CONTINUATION",
                         "RD_CE_OVER_SRST", "WR_WIDE_CONTINUATION"):
                if decode_parameter(params.get(name)) != 0:
                    unsupported_parameters.append(name)
            if unsupported_parameters:
                self.block("memory_semantics", "unsupported $mem_v2 features: " +
                           ", ".join(unsupported_parameters), src)
                continue
            for name in ("RD_ARST", "RD_SRST"):
                if any(bit != "0" for bit in connections.get(name, [])):
                    self.block("memory_read_control",
                               f"nonconstant {name} requires clocked-read lowering", src)
            clock_enable = params.get("WR_CLK_ENABLE", 0)
            clock_polarity = params.get("WR_CLK_POLARITY", 0)
            for port in range(write_ports):
                if self.packed_bit(clock_enable, port) != "1":
                    self.block("asynchronous_memory_write",
                               f"write port {port} is not clocked", src)
                if self.packed_bit(clock_polarity, port) != "1":
                    self.block("falling_edge_memory_write",
                               f"write port {port} is not rising-edge", src)
                clock_domains.add(((connections["WR_CLK"][port],), True))
            init_width = size * width
            init_bits = self.parameter_bits(params.get("INIT"), init_width)
            if init_bits is None:
                self.block("memory_initialization",
                           f"INIT is not an exact {init_width}-bit Yosys parameter", src)
                init_bits = ["x"] * init_width
            init_expr = self.constant(init_bits, src)
            if init_expr is None:
                self.block("memory_initialization", "INIT is not a constant", src)
                init_expr = literal(init_width, 0, src)
            loom_name = "__loom_mem_" + self.legal_identifier(cell_name)
            original_name = params.get("MEMID", cell_name)
            if not isinstance(original_name, str):
                original_name = cell_name
            original_name = original_name.removeprefix("\\")
            info = MemoryInfo(cell_name, original_name, src, size, width, abits, address_width,
                              read_ports, write_ports, connections, init_bits,
                              init_expr, loom_name)
            self.memory_cells[cell_name] = info
            if write_ports != 0:
                writable.append(info)

        # Only build write expressions after every memory output has been
        # indexed: one memory's write data may depend combinationally on a
        # different memory's read port.
        for info in writable:
            connections = info.connections
            packed_init = info.init_expr.get(
                "value", info.init_expr.get("partial", {}).get("implementation_value", 0))
            init = [((packed_init >> (address * info.width)) &
                     ((1 << info.width) - 1)) for address in range(info.size)]
            refinement = (info.init_expr.get("partial")
                          if info.init_expr.get("kind") == "partial_literal" else None)

            # Loom's core memory action writes a complete word.  Preserve a
            # Yosys per-bit write mask without adding backend semantics by
            # making each port's complete-word value include every earlier
            # enabled write to the same address.  All expressions still read
            # the pre-cycle memory; ordered whole-word commits then reproduce
            # the source's ordered, per-bit last-write-wins behavior exactly.
            ports: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                              tuple[str, ...] | None]] = []
            for port in range(info.write_ports):
                address, in_range = self.memory_address(info, port, True)
                raw_address = tuple(connections["WR_ADDR"][
                    port * info.abits:(port + 1) * info.abits])
                static_address = (raw_address if all(
                    bit in ("0", "1") for bit in raw_address) else None)
                mask = self.expr(connections["WR_EN"][
                    port * info.width:(port + 1) * info.width], info.source)
                if info.abits != info.address_width:
                    mask = {"kind": "mux", "width": info.width,
                            "condition": in_range, "yes": mask,
                            "no": literal(info.width, 0, info.source),
                            "source": info.source}
                data = self.expr(connections["WR_DATA"][
                    port * info.width:(port + 1) * info.width], info.source)
                ports.append((address, mask, data, static_address))

            # A leading run of literal-address ports can be grouped by exact
            # address. Writes to different literal addresses commute, while
            # writes in each group retain their original order. This common
            # Yosys lowering shape (including KianV's cache clear/refill
            # waves) avoids constructing comparisons that are statically
            # false and reduces two writes per cache entry to one Loom port.
            static_prefix = 0
            while (static_prefix < len(ports) and
                   ports[static_prefix][3] is not None):
                static_prefix += 1
            if static_prefix:
                grouped: dict[tuple[str, ...], tuple[
                    dict[str, Any], dict[str, Any], dict[str, Any], tuple[str, ...]]] = {}
                order: list[tuple[str, ...]] = []
                for address, mask, data, key in ports[:static_prefix]:
                    assert key is not None
                    if key not in grouped:
                        order.append(key)
                        grouped[key] = (address, mask, data, key)
                    else:
                        old_address, old_mask, old_data, _ = grouped[key]
                        inverse = unary(info.width, "bit_not", mask, info.source)
                        combined_data = binary(info.width, "bit_or",
                            binary(info.width, "bit_and", old_data, inverse,
                                   info.source),
                            binary(info.width, "bit_and", data, mask,
                                   info.source), info.source)
                        combined_mask = binary(info.width, "bit_or", old_mask,
                                               mask, info.source)
                        grouped[key] = (old_address, combined_mask,
                                        combined_data, key)
                ports = [grouped[key] for key in order] + ports[static_prefix:]

            writes = []
            for port, (address, mask, data, static_address) in enumerate(ports):
                accumulated = {"kind": "memory_read", "width": info.width,
                               "memory": info.loom_name, "address": address,
                               "source": info.source}
                for (earlier_address, earlier_mask, earlier_data,
                     earlier_static_address) in ports[:port]:
                    if (static_address is not None and
                            earlier_static_address is not None and
                            static_address != earlier_static_address):
                        continue
                    same_address = binary(1, "equal", earlier_address,
                                          address, info.source)
                    earlier_enabled = unary(1, "reduce_bool", earlier_mask,
                                            info.source)
                    applies = binary(1, "bit_and", same_address,
                                     earlier_enabled, info.source)
                    inverse = unary(info.width, "bit_not", earlier_mask,
                                    info.source)
                    merged = binary(info.width, "bit_or",
                                    binary(info.width, "bit_and", accumulated,
                                           inverse, info.source),
                                    binary(info.width, "bit_and", earlier_data,
                                           earlier_mask, info.source),
                                    info.source)
                    accumulated = {"kind": "mux", "width": info.width,
                                   "condition": applies, "yes": merged,
                                   "no": accumulated, "source": info.source}
                inverse = unary(info.width, "bit_not", mask, info.source)
                complete_data = binary(info.width, "bit_or",
                    binary(info.width, "bit_and", accumulated, inverse,
                           info.source),
                    binary(info.width, "bit_and", data, mask, info.source),
                    info.source)
                writes.append({"port": port,
                               "enable": unary(1, "reduce_bool", mask,
                                               info.source),
                               "address": address, "data": complete_data,
                               "source": info.source})
            memories.append({"name": info.loom_name,
                             "address_width": info.address_width,
                             "data_width": info.width, "init": init,
                             "init_refinement": refinement,
                             "writes": writes, "source": info.source})
        return memories

    def memory_equivalence(self) -> list[dict[str, Any]]:
        return [{"original": info.original_name, "size": info.size,
                 "width": info.width, "word_memory": info.loom_name}
                for info in self.memory_cells.values() if info.write_ports != 0]

    def cell_expr(self, cell_name: str, port: str) -> dict[str, Any]:
        key = (cell_name, port)
        if key in self.expression_cache:
            return self.expression_cache[key]
        cell = self.cells[cell_name]
        src = location(cell.get("attributes", {}).get("src", ""), self.module_source["file"])
        bits = cell.get("connections", {}).get(port, [])
        width = len(bits)
        if key in self.in_progress:
            self.block("combinational_cycle", f"cycle at {cell_name}.{port}", src)
            return literal(max(width, 1), 0, src)
        self.in_progress.add(key)
        kind = cell.get("type", "")
        connections = cell.get("connections", {})
        params = cell.get("parameters", {})
        if kind in SEQUENTIAL:
            name = self.registers.get(tuple(bits), self.public_name(bits, cell_name.strip("\\$")))
            result = signal(width, name, src)
        elif kind in self.modules:
            result = signal(max(width, 1), self.instance_net(cell_name, port), src)
        elif kind == "$mem_v2" and cell_name in self.memory_cells and port == "RD_DATA":
            info = self.memory_cells[cell_name]
            parts = [self.memory_read(info, read_port)
                     for read_port in reversed(range(info.read_ports))]
            result = parts[0]
            for part in parts[1:]:
                result = {"kind": "concat", "width": result["width"] + part["width"],
                          "high": result, "low": part, "source": src}
        elif kind.startswith("$mem"):
            self.block("memory_cell", f"unsupported memory output {kind}.{port}", src)
            result = literal(max(width, 1), 0, src)
        elif kind not in SUPPORTED_COMB:
            self.block("yosys_cell", f"unsupported cell type {kind}", src)
            result = literal(max(width, 1), 0, src)
        elif kind in ("$not", "$neg"):
            value = resize(self.expr(connections.get("A", []), src), width,
                           bool(decode_parameter(params.get("A_SIGNED"))), src)
            result = unary(width, SUPPORTED_COMB[kind], value, src)
        elif kind in ("$logic_not", "$reduce_and", "$reduce_or", "$reduce_bool"):
            value = self.expr(connections.get("A", []), src)
            reduced = unary(1, SUPPORTED_COMB[kind], value, src)
            result = resize(reduced, width, False, src)
        elif kind in ("$logic_and", "$logic_or"):
            left = booleanize(self.expr(connections.get("A", []), src), src)
            right = booleanize(self.expr(connections.get("B", []), src), src)
            reduced = binary(1, "bit_and" if kind == "$logic_and" else "bit_or",
                             left, right, src)
            result = resize(reduced, width, False, src)
        elif kind == "$mux":
            yes = resize(self.expr(connections.get("B", []), src), width, False, src)
            no = resize(self.expr(connections.get("A", []), src), width, False, src)
            condition = self.expr(connections.get("S", []), src)
            result = {"kind": "mux", "width": width, "condition": condition,
                      "yes": yes, "no": no, "source": src}
        elif kind == "$sshr":
            value = self.expr(connections.get("A", []), src)
            amount = self.expr(connections.get("B", []), src)
            result = arithmetic_shift_right(
                value, amount, width,
                bool(decode_parameter(params.get("A_SIGNED"))), src)
        elif kind == "$shift":
            value = self.expr(connections.get("A", []), src)
            amount_raw = self.expr(connections.get("B", []), src)
            value_signed = bool(decode_parameter(params.get("A_SIGNED")))
            amount_signed = bool(decode_parameter(params.get("B_SIGNED")))
            work_width = max(value["width"], amount_raw["width"], width)
            value = resize(value, work_width, value_signed, src)
            amount = resize(amount_raw, work_width, amount_signed, src)
            right = binary(work_width, "logical_shift_right", value, amount, src)
            if amount_signed:
                negative = slice_expr(amount, work_width - 1, 1, src)
                magnitude = unary(work_width, "negate", amount, src)
                left = binary(work_width, "shift_left", value, magnitude, src)
                shifted = {"kind": "mux", "width": work_width,
                           "condition": negative, "yes": left, "no": right,
                           "source": src}
            else:
                shifted = right
            result = resize(shifted, width, value_signed, src)
        elif kind == "$shiftx":
            value = self.expr(connections.get("A", []), src)
            amount = self.expr(connections.get("B", []), src)
            if bool(decode_parameter(params.get("B_SIGNED"))):
                self.block("signed_shiftx",
                           "signed variable part-select requires negative-index refinement", src)
                result = literal(max(width, 1), 0, src)
            else:
                site_payload = {"module": self.name, "cell": cell_name,
                                "kind": "$shiftx", "source": src}
                site = "four_state_" + sha256(json.dumps(
                    site_payload, sort_keys=True,
                    separators=(",", ":")).encode())[:24]
                matches = ([] if self.four_state_policy is None else
                           self.four_state_policy.matching(site, self.name, src))
                if len(matches) != 1:
                    kind_name = ("four_state_variable_part_select" if
                                 self.four_state_policy is None else
                                 ("four_state_policy_missing" if not matches else
                                  "four_state_policy_ambiguous"))
                    self.block(kind_name,
                               f"variable part-select {site} matched {len(matches)} policy rules",
                               src, {"site": site, "pattern": f"{width}'dynamic-x",
                                     "width": width, "unknown_bits": width})
                    result = literal(max(width, 1), 0, src)
                else:
                    rule = matches[0]
                    if rule["fill"] != "zero":
                        self.block(
                            "four_state_shiftx_fill",
                            "Yosys formal `$shiftx` only has a validated zero-fill normalization",
                            src, {"site": site, "pattern": f"{width}'dynamic-x",
                                  "width": width, "unknown_bits": width})
                    fill_value = ((1 << width) - 1
                                  if rule["fill"] == "one" else 0)
                    fill = {
                        "kind": "partial_literal", "width": width,
                        "partial": {
                            "site": site,
                            "classification": rule["classification"],
                            "known_mask": 0, "known_value": 0,
                            "implementation_value": fill_value,
                            "rationale": f"{rule['name']}: {rule['rationale']}",
                        },
                        "source": src,
                    }
                    extended = {"kind": "concat",
                                "width": value["width"] + width,
                                "high": fill, "low": value, "source": src}
                    work_width = max(extended["width"], amount["width"])
                    extended = resize(extended, work_width, False, src)
                    amount = resize(amount, work_width, False, src)
                    shifted = binary(work_width, "logical_shift_right",
                                     extended, amount, src)
                    selected = resize(shifted, width, False, src)
                    in_range = binary(
                        1, "unsigned_less_than", amount,
                        literal(work_width, value["width"], src), src)
                    result = {"kind": "mux", "width": width,
                              "condition": in_range, "yes": selected,
                              "no": fill, "source": src}
        else:
            left_signed = bool(decode_parameter(params.get("A_SIGNED")))
            right_signed = bool(decode_parameter(params.get("B_SIGNED")))
            op = SUPPORTED_COMB[kind]
            if kind in ("$eq", "$ne", "$lt", "$le", "$ge", "$gt"):
                operand_width = max(len(connections.get("A", [])), len(connections.get("B", [])))
                left = resize(self.expr(connections.get("A", []), src), operand_width,
                              left_signed, src)
                right = resize(self.expr(connections.get("B", []), src), operand_width,
                               right_signed, src)
                less_op = ("signed_less_than" if left_signed and right_signed
                           else "unsigned_less_than")
                if kind == "$eq":
                    compared = binary(1, "equal", left, right, src)
                elif kind == "$ne":
                    compared = unary(1, "logical_not",
                                     binary(1, "equal", left, right, src), src)
                elif kind == "$lt":
                    compared = binary(1, less_op, left, right, src)
                elif kind == "$gt":
                    compared = binary(1, less_op, right, left, src)
                elif kind == "$ge":
                    compared = unary(1, "logical_not",
                                     binary(1, less_op, left, right, src), src)
                else:  # $le
                    compared = unary(1, "logical_not",
                                     binary(1, less_op, right, left, src), src)
                result = resize(compared, width, False, src)
            else:
                left = resize(self.expr(connections.get("A", []), src), width,
                              left_signed, src)
                right = resize(self.expr(connections.get("B", []), src), width,
                               right_signed, src)
                result = binary(width, op, left, right, src)
        self.in_progress.remove(key)
        self.expression_cache[key] = result
        return result

    def translate(self) -> dict[str, Any]:
        registers = []
        clock_domains: set[tuple[tuple[Any, ...], bool]] = set()
        asynchronous_cells: list[str] = []
        sequential_cells = []
        for cell_name, cell in self.cells.items():
            kind = cell.get("type", "")
            if kind not in SEQUENTIAL:
                continue
            sequential_cells.append((cell_name, cell))
            connections = cell.get("connections", {})
            params = cell.get("parameters", {})
            q = connections.get("Q", [])
            original_name = self.register_original(q, cell_name.strip("\\$"))
            reg_name = "__loom_reg_" + self.legal_identifier(cell_name)
            self.registers[tuple(q)] = reg_name
            self.register_originals[tuple(q)] = original_name
            clock_domains.add((tuple(connections.get("CLK", [])),
                               bool(decode_parameter(params.get("CLK_POLARITY"), 1))))
            if kind.startswith("$adff"):
                asynchronous_cells.append(cell_name)
        if asynchronous_cells:
            self.block("asynchronous_reset_state",
                       "asynchronous reset state must remain behind an external contract: " +
                       ", ".join(sorted(asynchronous_cells)), self.module_source)

        memories = self.prepare_memories(clock_domains)
        ordered_domains = sorted(clock_domains, key=str)
        domain_names = {
            domain: (f"{self.name}_clock" if len(ordered_domains) == 1 else
                     f"{self.name}_clock_{index}")
            for index, domain in enumerate(ordered_domains)
        }

        for cell_name, cell in sequential_cells:
            kind = cell["type"]
            connections = cell.get("connections", {})
            params = cell.get("parameters", {})
            src = location(cell.get("attributes", {}).get("src", ""), self.module_source["file"])
            q = connections.get("Q", [])
            width = len(q)
            name = self.registers[tuple(q)]
            next_value = self.expr(connections.get("D", []), src)
            enable = None
            if kind in ("$dffe", "$sdffe", "$sdffce", "$adffe"):
                enable = self.expr(connections.get("EN", []), src)
                if not bool(decode_parameter(params.get("EN_POLARITY"), 1)):
                    enable = {"kind": "unary", "width": 1, "op": "bit_not",
                              "value": enable, "source": src}
            if kind.startswith("$sdff"):
                reset = self.expr(connections.get("SRST", []), src)
                if not bool(decode_parameter(params.get("SRST_POLARITY"), 1)):
                    reset = {"kind": "unary", "width": 1, "op": "bit_not",
                             "value": reset, "source": src}
                reset_value = literal(
                    width, decode_parameter(params.get("SRST_VALUE", 0)), src)
                reset_selected = {"kind": "mux", "width": width,
                                  "condition": reset, "yes": reset_value,
                                  "no": next_value, "source": src}
                if kind == "$sdffce":
                    next_value = {"kind": "mux", "width": width,
                                  "condition": enable, "yes": reset_selected,
                                  "no": signal(width, name, src), "source": src}
                else:
                    if enable is not None:
                        next_value = {"kind": "mux", "width": width,
                                      "condition": enable, "yes": next_value,
                                      "no": signal(width, name, src), "source": src}
                    next_value = {"kind": "mux", "width": width,
                                  "condition": reset, "yes": reset_value,
                                  "no": next_value, "source": src}
            elif enable is not None:
                next_value = {"kind": "mux", "width": width, "condition": enable,
                              "yes": next_value, "no": signal(width, name, src),
                              "source": src}
            register_domain = (domain_names[(tuple(connections.get("CLK", [])),
                                             bool(decode_parameter(
                                                 params.get("CLK_POLARITY"), 1)))]
                               if len(ordered_domains) > 1 else None)
            registers.append({"name": name, "width": width, "init": 0,
                              "next": next_value, "domain": register_domain,
                              "source": src})
            original, original_offset = self.register_originals[tuple(q)]
            self.register_equivalence.append({
                "original": original, "original_offset": original_offset,
                "loom": name, "width": width, "domain": register_domain})

        instances = []
        for cell_name, cell in self.cells.items():
            kind = cell.get("type", "")
            if kind not in self.modules:
                continue
            src = location(cell.get("attributes", {}).get("src", ""), self.module_source["file"])
            directions = cell.get("port_directions", {})
            if cell.get("parameters"):
                self.block("unelaborated_instance_parameter",
                           f"child instance {cell_name} retains parameter overrides", src)
            for port, bits in cell.get("connections", {}).items():
                if directions.get(port) == "input" and not bits:
                    self.block("unconnected_instance_input",
                               f"child input {cell_name}.{port} is unconnected", src)
            instances.append({
                "name": cell_name, "module_name": kind,
                "parameters": [{"name": name, "value": str(value)}
                               for name, value in sorted(cell.get("parameters", {}).items())],
                "connections": [
                    {"port": port, "direction": directions.get(port, "unknown"),
                     "signal": self.instance_net(cell_name, port),
                     "width": len(bits),
                     "value": self.expr(bits, src)
                        if directions.get(port) == "input" else None,
                     "source": src}
                    for port, bits in sorted(cell.get("connections", {}).items())
                    if bits or directions.get(port) != "output"
                ], "source": src,
            })

        for cell in self.cells.values():
            kind = cell.get("type", "")
            if (kind.startswith("$") and kind not in self.modules and
                    kind not in SEQUENTIAL and kind not in SUPPORTED_COMB and
                    not kind.startswith("$mem")):
                self.block("yosys_cell", f"unsupported cell type {kind}",
                           cell.get("attributes", {}).get("src", ""))

        ports = []
        for name, port in self.module.get("ports", {}).items():
            ports.append({"name": name, "direction": port.get("direction", "unknown"),
                          "width": len(port.get("bits", [])), "semantic_type": "bits",
                          "source": self.source_for_bits(port.get("bits", []))})

        domains = []
        for domain_key in ordered_domains:
            clock_bits, rising = domain_key
            clock_name = self.input_port_name(list(clock_bits), "clk")
            reset_record = {"kind": "resetless", "port": None, "active_high": True,
                            "source": None}
            domains.append({"name": domain_names[domain_key], "clock_port": clock_name,
                            "edge": "rising" if rising else "falling",
                            "reset": reset_record,
                            "source": self.source_for_bits(list(clock_bits))})
        # No inferred sequential cells means a genuinely stateless module.
        # The neutral IR records this as an empty domain list; Loom's checked
        # stateless lowering emits no synthetic clock/reset interface.

        outputs = []
        for name, port in self.module.get("ports", {}).items():
            if port.get("direction") == "output":
                src = self.source_for_bits(port.get("bits", []))
                outputs.append({"name": name, "width": len(port.get("bits", [])),
                                "value": self.expr(port.get("bits", []), src),
                                "source": src})
            elif port.get("direction") not in ("input", "output"):
                self.block("port_direction", f"unsupported direction on port {name}",
                           self.source_for_bits(port.get("bits", [])))

        return {"name": self.name, "ports": ports, "domains": domains,
                "registers": registers, "memories": memories, "outputs": outputs,
                "memory_equivalence": self.memory_equivalence(),
                "register_equivalence": self.register_equivalence,
                "instances": instances, "unsupported": self.unsupported,
                "source": self.module_source}


EXPRESSION_KINDS = {
    "literal", "partial_literal", "signal", "unary", "binary", "mux", "slice",
    "zero_extend", "sign_extend", "concat", "memory_read",
}


def encode_module_expression_dag(module: dict[str, Any]) -> dict[str, Any]:
    """Replace recursive expression trees with a shared postorder table.

    The adapter caches cell expressions, so the semantic representation is a
    DAG. Encoding it as ordinary recursive JSON duplicates shared cones and
    becomes exponential on wide SoC modules.
    """
    expressions: list[dict[str, Any]] = []
    ids: dict[int, int] = {}

    def intern(expression: dict[str, Any]) -> int:
        identity = id(expression)
        if identity in ids:
            return ids[identity]
        kind = expression.get("kind")
        if kind not in EXPRESSION_KINDS:
            raise ValueError(f"not an import expression: {kind!r}")
        node = dict(expression)
        if kind == "unary":
            node["value"] = intern(expression["value"])
        elif kind == "binary":
            node["left"] = intern(expression["left"])
            node["right"] = intern(expression["right"])
        elif kind == "mux":
            node["condition"] = intern(expression["condition"])
            node["yes"] = intern(expression["yes"])
            node["no"] = intern(expression["no"])
        elif kind in ("slice", "zero_extend", "sign_extend"):
            node["value"] = intern(expression["value"])
        elif kind == "concat":
            node["high"] = intern(expression["high"])
            node["low"] = intern(expression["low"])
        elif kind == "memory_read":
            node["address"] = intern(expression["address"])
        expression_id = len(expressions)
        ids[identity] = expression_id
        expressions.append(node)
        return expression_id

    encoded = dict(module)
    encoded["registers"] = [
        {**register, "next": intern(register["next"])}
        for register in module["registers"]
    ]
    encoded["memories"] = [
        {**memory, "writes": [
            {**write, "enable": intern(write["enable"]),
             "address": intern(write["address"]), "data": intern(write["data"])}
            for write in memory["writes"]
        ]}
        for memory in module["memories"]
    ]
    encoded["outputs"] = [
        {**output, "value": intern(output["value"])}
        for output in module["outputs"]
    ]
    encoded["instances"] = [
        {**instance, "connections": [
            {**connection,
             "value": (intern(connection["value"])
                       if connection["value"] is not None else None)}
            for connection in instance["connections"]
        ]}
        for instance in module["instances"]
    ]
    encoded["expressions"] = expressions
    return encoded


def main() -> int:
    # Wide Yosys vectors can become exact, deeply nested concat trees. Python's
    # default JSON recursion limit is too small for real SoC packages.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 100_000))
    parser = argparse.ArgumentParser()
    parser.add_argument("--yosys-json", type=pathlib.Path, required=True)
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--module")
    selection.add_argument("--package-top")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--manifest-output", type=pathlib.Path)
    parser.add_argument("--four-state-policy", type=pathlib.Path)
    args = parser.parse_args()

    yosys_bytes = args.yosys_json.read_bytes()
    inventory_bytes = args.inventory.read_bytes()
    inventory = json.loads(inventory_bytes)
    expected = inventory.get("frontend", {}).get("elaborated_sha256")
    actual = sha256(yosys_bytes)
    if expected != actual:
        raise SystemExit(f"elaborated JSON identity mismatch: inventory={expected} actual={actual}")
    design = json.loads(yosys_bytes)
    policy_bytes = args.four_state_policy.read_bytes() if args.four_state_policy else None
    try:
        four_state_policy = (FourStatePolicy(json.loads(policy_bytes))
                             if policy_bytes is not None else None)
    except (ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid four-state policy: {error}") from error
    modules = design.get("modules", {})
    selected_top = args.module or args.package_top
    if selected_top not in modules:
        raise SystemExit(f"module not found in elaborated design: {selected_top}")
    common_frontend = {
        "name": "yosys-json",
        "version": inventory.get("frontend", {}).get("tool", "unknown"),
        "source_set_sha256": inventory.get("source_set_sha256", ""),
        "inventory_sha256": sha256(inventory_bytes),
        "elaborated_sha256": actual,
        "assumptions": [
            "Yosys correctly parsed and elaborated the identified RTL",
            "this adapter correctly translated supported Yosys cells",
            "original-vs-emitted formal equivalence remains required",
        ],
    }
    if four_state_policy is not None:
        common_frontend["assumptions"].append(
            "the identified four-state policy classifies and concretizes every partial value")
    if args.module:
        translated = ModuleTranslator(args.module, modules[args.module], modules,
                                      four_state_policy).translate()
        translated_modules = [translated]
        report = {"schema": 1, "frontend": common_frontend, "module": translated}
    else:
        reachable = {args.package_top}
        pending = [args.package_top]
        while pending:
            parent = pending.pop()
            for cell in modules[parent].get("cells", {}).values():
                child = cell.get("type")
                if child in modules and child not in reachable:
                    reachable.add(child)
                    pending.append(child)
        translated_modules = []
        for name in sorted(reachable):
            module = modules[name]
            translated = ModuleTranslator(name, module, modules,
                                          four_state_policy).translate()
            translated_modules.append(encode_module_expression_dag(translated))
        report = {
            "schema": 2,
            "frontend": common_frontend,
            "package": {
                "top": args.package_top,
                "modules": translated_modules,
                "source": location(
                    modules[args.package_top].get("attributes", {}).get("src", "")),
            },
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    manifest_path = args.manifest_output or pathlib.Path(str(args.output) + ".manifest.json")
    source_root = pathlib.Path(inventory.get("source_root", "."))
    source_artifacts = []
    for source in inventory.get("sources", []):
        path = pathlib.Path(source.get("path", ""))
        resolved = path if path.is_absolute() else source_root / path
        source_artifacts.append({
            "role": "source",
            "path": resolved.resolve().as_posix(),
            "sha256": source.get("sha256", ""),
            "bytes": source.get("bytes", 0),
        })
    policy_artifacts = ([] if args.four_state_policy is None else [{
        "role": "four_state_policy",
        "path": args.four_state_policy.resolve().as_posix(),
        "sha256": sha256(policy_bytes), "bytes": len(policy_bytes),
    }])
    manifest = {
        "schema": 1,
        "module": selected_top,
        "frontend": "scripts/yosys_to_loom_ir.py",
        "version": inventory.get("frontend", {}).get("tool", "unknown"),
        "invocation": sys.argv,
        "artifacts": source_artifacts + policy_artifacts + [
            {"role": "inventory", "path": args.inventory.resolve().as_posix(),
             "sha256": sha256(inventory_bytes), "bytes": len(inventory_bytes)},
            {"role": "elaborated_yosys_json", "path": args.yosys_json.resolve().as_posix(),
             "sha256": actual, "bytes": len(yosys_bytes)},
            {"role": "neutral_import_ir", "path": args.output.resolve().as_posix(),
             "sha256": sha256(args.output.read_bytes()),
             "bytes": args.output.stat().st_size},
        ],
        "assumptions": common_frontend["assumptions"],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    unsupported_count = sum(len(module["unsupported"]) for module in translated_modules)
    status = "PASS" if unsupported_count == 0 else "BLOCKED"
    kind = "module" if args.module else "package"
    print(f"YOSYS_TO_LOOM_IR_{status} {kind}={selected_top} "
          f"unsupported={unsupported_count} sha256={sha256(args.output.read_bytes())} "
          f"manifest={manifest_path}")
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
