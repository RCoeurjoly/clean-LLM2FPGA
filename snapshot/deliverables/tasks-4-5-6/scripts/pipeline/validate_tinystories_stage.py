"""Validate generated stages without comparing them to frozen outputs."""

import json
from pathlib import Path
import re
from typing import Mapping


class StageError(ValueError):
    """An artifact does not satisfy its declared stage contract."""


def validate_stage(kind: str, output: Path, inputs: Mapping[str, Path]) -> dict[str, object]:
    if kind not in {"calyx", "image", "synthesis", "placement", "route"}:
        raise StageError(f"STAGE_KIND: unsupported stage {kind}")

    def required(name: str) -> Path:
        path = output / name
        if not path.is_file() or path.stat().st_size == 0:
            raise StageError(f"STAGE_MISSING: {kind}: {name}")
        return path

    def obj(name: str) -> dict:
        try:
            value = json.loads(required(name).read_text())
        except (ValueError, UnicodeError) as exc:
            raise StageError(f"STAGE_JSON: {kind}: {name}") from exc
        if not isinstance(value, dict):
            raise StageError(f"STAGE_JSON: {kind}: {name} is not an object")
        return value

    if kind == "calyx":
        futil = required("model.futil").read_text()
        control = required("control.sv").read_text()
        variant = required("model.sv").read_text()
        if not re.search(r"\bcomponent\s+main\b", futil):
            raise StageError("CALYX_MAIN: missing main component")
        if not re.search(r"\bmodule\s+main\b", variant):
            raise StageError("CALYX_SV_MAIN: missing main module")
        if not re.search(r"\bmodule\s+main\b", control):
            raise StageError("CALYX_CONTROL_MAIN: missing control main module")
        receipt = obj("receipt.json")
        if receipt.get("changed_memory") != "c_proj_weight_codes_i8":
            raise StageError("CALYX_VARIANT: wrong memory mapping")
        return {"kind": kind, "source_memory_count": None}

    if kind == "image":
        sv = required("model-static-init-normalized.sv").read_text()
        if not re.search(r"\bmodule\s+", sv):
            raise StageError("IMAGE_SV: no module")
        manifest = obj("manifest.json")
        records = manifest.get("source_memories")
        if not isinstance(records, dict) or not records:
            raise StageError("IMAGE_MEMORIES: absent source memories")
        if "postpass" in inputs:
            provenance_path = inputs["postpass"] / "generator-provenance.json"
            try:
                provenance = json.loads(provenance_path.read_text())
                compiler_memories = provenance["source_memories"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise StageError("IMAGE_SOURCE_SET: compiler provenance unavailable") from exc
            if not isinstance(compiler_memories, dict) or set(records) != set(compiler_memories):
                raise StageError("IMAGE_SOURCE_SET: image and compiler memories differ")
        expected = {f"image-{name}.hex" for name in records}
        actual = {path.name for path in output.glob("image-*.hex")}
        if actual != expected:
            raise StageError(f"IMAGE_FILES: missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
        for name, record in records.items():
            if not isinstance(record, dict):
                raise StageError(f"IMAGE_RECORD: {name}")
            width, elements = record.get("width"), record.get("elements")
            if not isinstance(width, int) or width < 1 or not isinstance(elements, int) or elements < 1:
                raise StageError(f"IMAGE_RECORD: {name}: invalid geometry")
            digits = (width + 3) // 4
            rows = (output / f"image-{name}.hex").read_text().splitlines()
            if len(rows) != elements or any(
                len(row) != digits or not re.fullmatch(r"[0-9a-fA-F]+", row) or int(row, 16) >= 1 << width
                for row in rows
            ):
                raise StageError(f"IMAGE_ROWS: {name}: expected {elements} rows of {digits} hex digits")
        return {"kind": kind, "source_memory_count": len(records)}

    if kind == "synthesis":
        netlist = obj("design.json")
        receipt = obj("receipt.json")
        top = receipt.get("top")
        modules = netlist.get("modules")
        if not isinstance(modules, dict) or top not in modules:
            raise StageError("SYNTHESIS_TOP: top module absent")
        if not isinstance(modules[top].get("cells"), dict) or not modules[top]["cells"]:
            raise StageError("SYNTHESIS_CELLS: empty top")
        return {"kind": kind, "top": top}

    if kind == "placement":
        netlist = obj("placed.json")
        if not isinstance(netlist.get("modules"), dict) or not netlist["modules"]:
            raise StageError("PLACEMENT_NETLIST: no modules")
        receipt = obj("receipt.json")
        if receipt.get("clock_mhz") != 30 or receipt.get("seed") != 15:
            raise StageError("PLACEMENT_PROFILE: clock or seed differs")
        return {"kind": kind, "clock_mhz": 30}

    obj("routed.json")
    required("routed.fasm")
    receipt = obj("receipt.json")
    iterations = receipt.get("route_iterations")
    if not isinstance(iterations, list) or not iterations:
        raise StageError("ROUTE_ITERATIONS: absent")
    final = iterations[-1]
    if final.get("overuse") != 0 or final.get("archfail") != 0:
        raise StageError("ROUTE_OVERUSE: routing conflicts remain")
    if receipt.get("covered_accelerator_clock_mhz", 0) < receipt.get("clock_mhz", 30):
        raise StageError("ROUTE_TIMING: covered paths miss clock")
    return {"kind": kind, "covered_clock_mhz": receipt["covered_accelerator_clock_mhz"]}
