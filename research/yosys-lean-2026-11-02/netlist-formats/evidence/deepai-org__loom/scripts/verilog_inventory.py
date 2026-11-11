#!/usr/bin/env python3
# Copyright (c) 2026 Kevin Baragona
# SPDX-License-Identifier: Apache-2.0
"""Fail-closed synthesizable-Verilog construct inventory.

This is a frontend *adapter*, not part of Loom's trusted semantics.  It asks
Yosys to elaborate one identified top, lowers processes, and classifies the
resulting modules/cells.  A frontend error produces no PASS report.  Every
report binds the exact source bytes, options, Yosys version, and elaborated
JSON bytes so later import/equivalence evidence cannot silently use a
different design.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile
from collections import Counter
from typing import Any


SEQUENTIAL = {
    "$dff": ("rising_or_falling", "none"),
    "$dffe": ("rising_or_falling", "none"),
    "$sdff": ("rising_or_falling", "synchronous"),
    "$sdffe": ("rising_or_falling", "synchronous"),
    "$sdffce": ("rising_or_falling", "synchronous"),
    "$adff": ("rising_or_falling", "asynchronous"),
    "$adffe": ("rising_or_falling", "asynchronous"),
    "$aldff": ("rising_or_falling", "asynchronous_load"),
    "$aldffe": ("rising_or_falling", "asynchronous_load"),
}
LATCHES = {"$dlatch", "$adlatch", "$dlatchsr"}
MEMORY_PREFIXES = ("$mem",)

# Directly representable after width/sign normalization in Loom's current
# Expr/Act core.  Everything else is an explicit importer blocker until a
# frontend normalization or a new proved constructor accounts for it.
LOOM_NATIVE_CELLS = {
    "$sdff", "$sdffe",
    "$not", "$neg", "$and", "$or", "$xor", "$add", "$sub", "$mul",
    "$div", "$mod", "$shl", "$shr", "$eq", "$lt", "$mux",
    "$logic_and", "$logic_or", "$logic_not", "$reduce_and", "$reduce_or",
    "$reduce_bool", "$ne", "$ge", "$gt", "$le",
}

SV_FEATURES = {
    "always_comb": re.compile(r"\balways_comb\b"),
    "always_ff": re.compile(r"\balways_ff\b"),
    "always_latch": re.compile(r"\balways_latch\b"),
    "logic": re.compile(r"\blogic\b"),
    "genvar": re.compile(r"\bgenvar\b"),
    "generate": re.compile(r"\bgenerate\b"),
    "indexed_part_select": re.compile(r"\[[^\]]+\s*[+:]-?\s*:\s*[^\]]+\]"),
    "typedef": re.compile(r"\btypedef\b"),
    "enum": re.compile(r"\benum\b"),
    "struct": re.compile(r"\bstruct\b"),
    "interface": re.compile(r"\binterface\b"),
    "package": re.compile(r"\bpackage\b"),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bit_parameter(value: Any, default: bool = True) -> bool:
    if isinstance(value, int):
        return value != 0
    if not isinstance(value, str) or not value:
        return default
    clean = value.replace("x", "0").replace("z", "0")
    try:
        return int(clean, 2) != 0
    except ValueError:
        return default


def source_path(src: str) -> str:
    # Yosys locations are PATH:line.col-line.col.  A Windows drive letter is
    # not relevant to the Linux ASIC flows this adapter currently targets.
    return src.split(":", 1)[0] if src else ""


def normalized_path(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def signal_names(module: dict[str, Any]) -> dict[Any, str]:
    candidates: dict[Any, list[str]] = {}
    for name, net in module.get("netnames", {}).items():
        for bit in net.get("bits", []):
            if isinstance(bit, int):
                candidates.setdefault(bit, []).append(name)
    result: dict[Any, str] = {}
    for bit, names in candidates.items():
        public = [name for name in names if not name.startswith("$")]
        choices = public or names
        result[bit] = sorted(choices, key=lambda name: (len(name), name))[0]
    return result


def connection_name(bits: list[Any], names: dict[Any, str]) -> str:
    rendered = []
    for bit in bits:
        if isinstance(bit, str):
            rendered.append(bit)
        else:
            rendered.append(names.get(bit, f"bit:{bit}"))
    return ",".join(rendered)


def edge(cell: dict[str, Any]) -> str:
    polarity = cell.get("parameters", {}).get("CLK_POLARITY", "1")
    return "rising" if bit_parameter(polarity) else "falling"


def active_level(cell: dict[str, Any], parameter: str) -> str:
    value = cell.get("parameters", {}).get(parameter)
    if value is None:
        return "unspecified"
    return "active_high" if bit_parameter(value) else "active_low"


def module_record(name: str, module: dict[str, Any], all_modules: dict[str, Any],
                  root: pathlib.Path, features: dict[str, list[str]]) -> dict[str, Any]:
    cells = module.get("cells", {})
    names = signal_names(module)
    clocks: list[dict[str, str]] = []
    resets: list[dict[str, str]] = []
    instances: list[dict[str, Any]] = []
    cell_counts = Counter(cell.get("type", "") for cell in cells.values())

    for cell_name, cell in sorted(cells.items()):
        kind = cell.get("type", "")
        if kind in SEQUENTIAL:
            clocks.append({
                "cell": cell_name,
                "signal": connection_name(cell.get("connections", {}).get("CLK", []), names),
                "edge": edge(cell),
                "source": cell.get("attributes", {}).get("src", ""),
            })
            reset_kind = SEQUENTIAL[kind][1]
            if reset_kind != "none":
                port = "ARST" if reset_kind.startswith("asynchronous") else "SRST"
                resets.append({
                    "cell": cell_name,
                    "kind": reset_kind,
                    "signal": connection_name(cell.get("connections", {}).get(port, []), names),
                    "level": active_level(cell, f"{port}_POLARITY"),
                    "source": cell.get("attributes", {}).get("src", ""),
                })
        if kind in all_modules:
            target = all_modules.get(kind, {})
            instances.append({
                "name": cell_name,
                "module": kind,
                "blackbox": bit_parameter(target.get("attributes", {}).get("blackbox", "0"), False),
                "source": cell.get("attributes", {}).get("src", ""),
            })

    attrs = module.get("attributes", {})
    src = attrs.get("src", "")
    src_file = source_path(src)
    if src_file:
        src_path = pathlib.Path(src_file)
        if not src_path.is_absolute():
            src_path = root / src_path
        rel_src = normalized_path(src_path, root)
    else:
        rel_src = ""
    unique_clocks = sorted({(item["signal"], item["edge"]) for item in clocks})
    unsupported = sorted(kind for kind in cell_counts
                         if kind.startswith("$") and kind not in all_modules and
                         kind not in LOOM_NATIVE_CELLS)
    memory_cells = sorted(kind for kind in cell_counts
                          if kind.startswith(MEMORY_PREFIXES))
    latch_cells = sorted(kind for kind in cell_counts if kind in LATCHES)
    sequential_cells = sum(cell_counts[kind] for kind in SEQUENTIAL)
    memory_count = sum(cell_counts[kind] for kind in memory_cells)
    combinational_count = sum(
        count for kind, count in cell_counts.items()
        if kind.startswith("$") and kind not in all_modules and kind not in SEQUENTIAL and
        kind not in LATCHES and not kind.startswith(MEMORY_PREFIXES)
    )
    ports = [{
        "name": port_name,
        "direction": port.get("direction", "unknown"),
        "width": len(port.get("bits", [])),
    } for port_name, port in module.get("ports", {}).items()]
    return {
        "name": name,
        "source": src,
        "source_file": rel_src,
        "blackbox": bit_parameter(attrs.get("blackbox", "0"), False),
        "ports": ports,
        "clock_domains": [{"signal": signal, "edge": polarity}
                          for signal, polarity in unique_clocks],
        "clocked_cells": clocks,
        "resets": resets,
        "sequential_cell_count": sequential_cells,
        "memory_cell_count": memory_count,
        "memory_cell_types": memory_cells,
        "latch_cell_count": sum(cell_counts[kind] for kind in latch_cells),
        "latch_cell_types": latch_cells,
        "combinational_cell_count": combinational_count,
        "instances": instances,
        "cell_types": dict(sorted(cell_counts.items())),
        "unsupported_import_cells": unsupported,
        "systemverilog_features": features.get(rel_src, []),
    }


def markdown(report: dict[str, Any]) -> str:
    rows = []
    for module in report["modules"]:
        clocks = ", ".join(
            f"{item['edge']}:{item['signal']}" for item in module["clock_domains"]
        ) or "—"
        reset_kinds = ", ".join(sorted({item["kind"] for item in module["resets"]})) or "—"
        unsupported = ", ".join(module["unsupported_import_cells"]) or "—"
        rows.append(
            f"| `{module['name']}` | {clocks} | {reset_kinds} | "
            f"{module['memory_cell_count']} | {module['latch_cell_count']} | "
            f"{len(module['instances'])} | {unsupported} |"
        )
    summary = report["summary"]
    return "\n".join([
        "# Verilog construct inventory",
        "",
        "This report is generated by `scripts/verilog_inventory.py`. Yosys is an",
        "untrusted elaboration adapter; `PASS` means elaboration and classification",
        "completed for the exact hashed inputs, not that Loom proved HDL equivalence.",
        "",
        f"- Frontend: **{report['frontend']['status']}**, `{report['frontend']['tool']}`",
        f"- Top: `{report['top']}`",
        f"- Source set SHA-256: `{report['source_set_sha256']}`",
        f"- Elaborated JSON SHA-256: `{report['frontend']['elaborated_sha256']}`",
        f"- Reachable modules: {summary['module_count']}",
        f"- Rising-edge domains: {summary['rising_edge_modules']}",
        f"- Falling-edge domains: {summary['falling_edge_modules']}",
        f"- Modules with inferred latches: {summary['latch_modules']}",
        f"- Modules with memories: {summary['memory_modules']}",
        f"- Modules with unsupported importer cell types: {summary['unsupported_cell_modules']}",
        f"- Modules blocked by the current importer for any reason: {summary['blocked_modules']}",
        "",
        "| Module | Clock edge(s) | Reset cell(s) | Memories | Latches | Instances | Import blockers |",
        "|---|---|---|---:|---:|---:|---|",
        *rows,
        "",
        "The JSON companion retains ports, source locations, individual clock/reset",
        "cells, complete cell counts, child instances, and source hashes.",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", required=True)
    parser.add_argument("--source-root", type=pathlib.Path, required=True)
    parser.add_argument("--source", action="append", type=pathlib.Path, required=True)
    parser.add_argument("--include", action="append", type=pathlib.Path, default=[])
    parser.add_argument("--define", action="append", default=[])
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--json-out", type=pathlib.Path, required=True)
    parser.add_argument("--markdown-out", type=pathlib.Path, required=True)
    parser.add_argument("--elaborated-out", type=pathlib.Path)
    args = parser.parse_args()

    root_argument = args.source_root
    root = root_argument.resolve()
    sources = [(root / path).resolve() if not path.is_absolute() else path.resolve()
               for path in args.source]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        parser.error("missing source file(s): " + ", ".join(missing))

    source_entries = []
    feature_map: dict[str, list[str]] = {}
    aggregate = hashlib.sha256()
    for path in sorted(sources):
        data = path.read_bytes()
        rel = normalized_path(path, root)
        digest = sha256(data)
        source_entries.append({"path": rel, "sha256": digest, "bytes": len(data)})
        aggregate.update(rel.encode())
        aggregate.update(b"\0")
        aggregate.update(bytes.fromhex(digest))
        text = data.decode("utf-8", errors="replace")
        feature_map[rel] = sorted(name for name, pattern in SV_FEATURES.items()
                                  if pattern.search(text))

    version_run = subprocess.run([args.yosys, "-V"], text=True,
                                 capture_output=True, check=False)
    if version_run.returncode:
        raise SystemExit(f"Yosys version query failed: {version_run.stderr}")
    version = version_run.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="loom-verilog-inventory-") as temp:
        temp_path = pathlib.Path(temp)
        elaborated = temp_path / "elaborated.json"
        options = ["-sv"]
        for include in args.include:
            include_path = (root / include).resolve() if not include.is_absolute() else include.resolve()
            options.append(f"-I{normalized_path(include_path, root)}")
        for define in args.define:
            options.append(f"-D{define}")
        # Yosys's command parser does not provide a stable cross-version quoted
        # path form. Reject whitespace instead of risking a changed source set.
        all_paths = [normalized_path(path, root) for path in sources]
        if any(any(char.isspace() for char in path) for path in all_paths):
            parser.error("source paths containing whitespace are unsupported")
        quoted_paths = " ".join(all_paths)
        script = "\n".join([
            f"read_verilog {' '.join(options)} {quoted_paths}",
            f"hierarchy -check -top {args.top}",
            "proc",
            "opt_dff",
            "opt_clean",
            # Normalize priority muxes to ordinary mux trees. The adapter still
            # consumes and validates the resulting neutral expression tree, and
            # original-vs-emitted equivalence remains a required external gate.
            "pmuxtree",
            "opt_clean",
            "memory_collect",
            "check",
            f"write_json {elaborated}",
        ])
        run = subprocess.run([args.yosys, "-Q", "-p", script], text=True,
                             capture_output=True, check=False, cwd=root)
        if run.returncode:
            raise SystemExit("Yosys elaboration failed closed:\n" +
                             run.stdout + run.stderr)
        data = elaborated.read_bytes()
        design = json.loads(data)

    if args.elaborated_out is not None:
        args.elaborated_out.parent.mkdir(parents=True, exist_ok=True)
        args.elaborated_out.write_bytes(data)

    modules_dict = design.get("modules", {})
    modules = [module_record(name, module, modules_dict, root, feature_map)
               for name, module in sorted(modules_dict.items())]
    summary = {
        "module_count": len(modules),
        "rising_edge_modules": sum(any(c["edge"] == "rising" for c in m["clock_domains"])
                                   for m in modules),
        "falling_edge_modules": sum(any(c["edge"] == "falling" for c in m["clock_domains"])
                                    for m in modules),
        "latch_modules": sum(m["latch_cell_count"] > 0 for m in modules),
        "memory_modules": sum(m["memory_cell_count"] > 0 for m in modules),
        "blackbox_modules": sum(m["blackbox"] for m in modules),
        "unsupported_cell_modules": sum(bool(m["unsupported_import_cells"]) for m in modules),
        "blocked_modules": sum(
            bool(m["unsupported_import_cells"]) or m["memory_cell_count"] > 0 or
            m["latch_cell_count"] > 0 or m["blackbox"] or not m["clock_domains"]
            for m in modules
        ),
    }
    report = {
        "schema": 1,
        "top": args.top,
        "frontend": {
            "status": "PASS",
            "tool": version,
            "adapter": "scripts/verilog_inventory.py",
            "elaborated_sha256": sha256(data),
            "assumptions": [
                "Yosys correctly parses and elaborates the identified source bytes and options",
                "cell classification does not establish behavioral equivalence",
            ],
        },
        "source_root": root_argument.as_posix(),
        "source_set_sha256": aggregate.hexdigest(),
        "sources": source_entries,
        "defines": args.define,
        "includes": [normalized_path((root / path).resolve() if not path.is_absolute()
                                     else path.resolve(), root)
                     for path in args.include],
        "summary": summary,
        "modules": modules,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(markdown(report))
    print(f"VERILOG_INVENTORY_PASS modules={len(modules)} "
          f"source_sha256={report['source_set_sha256']} "
          f"elaborated_sha256={report['frontend']['elaborated_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
