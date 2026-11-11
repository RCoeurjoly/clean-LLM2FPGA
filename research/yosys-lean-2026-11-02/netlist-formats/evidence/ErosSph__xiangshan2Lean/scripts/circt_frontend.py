"""Pinned CIRCT normalization and strict reader for its generic MLIR output.

The official compiler parses the source FIRRTL and resolves widths, aggregates,
whens and connection priority. This reader consumes SSA identities, never the
human-readable FIRRTL export (whose names need not be unique).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import os
from pathlib import Path
import re

from common import ROOT, FIRTOOL_VERSION, run_logged


def mlir_string(value: str) -> str:
    if not value.startswith('"') or not value.endswith('"'):
        raise ValueError("Expected an MLIR string: " + value)
    result = bytearray()
    index = 1
    while index < len(value) - 1:
        char = value[index]
        if char == "\\":
            index += 1
            char = value[index]
            if char in {'"', "\\"}:
                result.extend(char.encode())
            elif re.fullmatch(r"[0-9A-Fa-f]{2}", value[index:index + 2]):
                result.append(int(value[index:index + 2], 16))
                index += 1
            else:
                raise ValueError("Unsupported MLIR string escape")
        else:
            result.extend(char.encode("utf-8"))
        index += 1
    return result.decode("utf-8")


def split_top(text: str, separator: str = ",") -> list[str]:
    parts, stack = [], []
    start, quoted, escaped = 0, False, False
    closing = {"(": ")", "[": "]", "{": "}", "<": ">"}
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in closing:
            stack.append(closing[char])
        elif char in ")]}>" and not (char == ">" and i and text[i - 1] == "-"):
            if not stack or stack.pop() != char:
                raise ValueError("Unbalanced MLIR value: " + text)
        elif char == separator and not stack:
            parts.append(text[start:i].strip())
            start = i + 1
    if stack or quoted:
        raise ValueError("Unterminated MLIR value: " + text)
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts


def attribute(text: str):
    text = text.strip()
    if text.startswith('"'):
        return mlir_string(text)
    if text.startswith("[") and text.endswith("]"):
        return [attribute(x) for x in split_top(text[1:-1])]
    if text.startswith("array<i1"):
        return [x.strip() == "true" for x in text.partition(":")[2].rstrip(">").split(",") if x.strip()]
    if text in {"true", "false"}:
        return text == "true"
    if text.startswith("@"):
        return {"symbol": mlir_string(text[1:]) if text[1:].startswith('"') else text[1:]}
    if match := re.fullmatch(r"(-?\d+)\s*:\s*([su]?i\d+|index)", text):
        return {"integer": match[1], "type": match[2]}
    return text


@dataclass
class Operation:
    name: str
    results: list[str]
    operands: list[str]
    attributes: dict
    input_types: list[str]
    result_types: list[str]
    regions: list[dict]
    source: dict


class GenericMLIR:
    """Read the generic operation grammar emitted by pinned official CIRCT.

    No custom dialect syntax or unconsumed text is accepted. Hardware support is
    checked separately; successfully reading an operation does not authorize it.
    """
    def __init__(self, text: str, file: str):
        self.text, self.file, self.pos = text, file, 0

    def skip(self):
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text.startswith("//", self.pos):
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end < 0 else end + 1
            else:
                break

    def take(self, value):
        self.skip()
        if not self.text.startswith(value, self.pos):
            raise ValueError(f"{self.file}:{self.text.count(chr(10), 0, self.pos) + 1}: expected {value!r}")
        self.pos += len(value)

    def peek(self, value):
        self.skip()
        return self.text.startswith(value, self.pos)

    def quoted(self):
        self.skip()
        start = self.pos
        self.take('"')
        escaped = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return self.text[start:self.pos]
        raise ValueError("Unterminated MLIR string")

    def group(self, opening, closing):
        self.take(opening)
        start, depth = self.pos, 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == '"':
                self.quoted()
                continue
            self.pos += 1
            if char == opening:
                depth += 1
            elif char == closing and not (closing == ">" and self.text[self.pos - 2] == "-"):
                depth -= 1
                if not depth:
                    return self.text[start:self.pos - 1]
        raise ValueError("Unterminated MLIR group")

    def dictionary(self):
        result = {}
        for part in split_top(self.group("{", "}")):
            key, equal, value = part.partition("=")
            key = key.strip()
            if key.startswith('"'):
                key = mlir_string(key)
            if key in result:
                raise ValueError("Duplicate MLIR attribute " + key)
            result[key] = attribute(value) if equal else {"unit": True}
        return result

    def identifier(self):
        self.skip()
        match = re.match(r"[%^][-a-zA-Z0-9_.$]+(?:#\d+)?", self.text[self.pos:])
        if not match:
            raise ValueError("Expected MLIR SSA/block identity")
        self.pos += len(match[0])
        return match[0]

    def types(self):
        if self.peek("("):
            return split_top(self.group("(", ")"))
        self.skip()
        start = self.pos
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if char == "<":
                self.group("<", ">")
            elif char.isspace() or char in ",)":
                break
            else:
                self.pos += 1
        if self.pos == start:
            raise ValueError("Expected MLIR result type")
        return [self.text[start:self.pos]]

    def region(self):
        self.take("{")
        arguments, operations = [], []
        if self.peek("^"):
            self.identifier()
            for part in split_top(self.group("(", ")")) if self.peek("(") else []:
                name, colon, tpe = part.partition(":")
                if not colon:
                    raise ValueError("Malformed MLIR block argument")
                arguments.append({"name": name.strip(), "type": tpe.strip().partition(" loc(")[0]})
            self.take(":")
        while not self.peek("}"):
            operations.append(asdict(self.operation()))
        self.take("}")
        return {"arguments": arguments, "operations": operations}

    def operation(self):
        self.skip()
        start, results = self.pos, []
        if self.peek("%"):
            while True:
                name = self.identifier()
                if self.peek(":"):
                    self.take(":")
                    self.skip()
                    match = re.match(r"\d+", self.text[self.pos:])
                    if not match:
                        raise ValueError("Expected MLIR result count")
                    self.pos += len(match[0])
                    results.extend(name + "#" + str(i) for i in range(int(match[0])))
                else:
                    results.append(name)
                if not self.peek(","):
                    break
                self.take(",")
            self.take("=")
        name = mlir_string(self.quoted())
        operands = split_top(self.group("(", ")"))
        attrs, regions = {}, []
        if self.peek("<"):
            self.take("<")
            attrs.update(self.dictionary())
            self.take(">")
        if self.peek("("):
            self.take("(")
            regions.append(self.region())
            while self.peek(","):
                self.take(",")
                regions.append(self.region())
            self.take(")")
        if self.peek("{"):
            other = self.dictionary()
            if attrs.keys() & other.keys():
                raise ValueError("Conflicting operation attributes")
            attrs.update(other)
        self.take(":")
        inputs = self.types()
        self.take("->")
        outputs = self.types()
        source = {"file": self.file, "line": self.text.count("\n", 0, start) + 1, "column": 1}
        if self.peek("loc("):
            self.take("loc")
            location = self.group("(", ")")
            if match := re.search(r'("(?:[^"\\]|\\.)*"):(\d+):(\d+)', location):
                source = {"file": mlir_string(match[1]), "line": int(match[2]), "column": int(match[3])}
        if len(results) != len(outputs) or len(operands) != len(inputs):
            raise ValueError("MLIR result/operand type arity mismatch for " + name)
        return Operation(name, results, operands, attrs, inputs, outputs, regions, source)

    def read(self):
        root = asdict(self.operation())
        self.skip()
        if self.pos != len(self.text):
            raise ValueError("Unconsumed MLIR text")
        return root


def circuit(root):
    if root["name"] != "builtin.module" or len(root["regions"]) != 1:
        raise ValueError("Expected the CIRCT builtin.module container")
    operations = root["regions"][0]["operations"]
    if len(operations) != 1 or operations[0]["name"] != "firrtl.circuit":
        raise ValueError("Expected exactly one FIRRTL circuit; other top-level dialects are unsupported")
    return operations[0]


def reject(op, message):
    s = op["source"]
    raise ValueError(f"{s['file']}:{s['line']}:{s['column']}: CIRCT: {message}")


def preflight(root, top=None):
    c = circuit(root)
    if len(c["regions"]) != 1:
        reject(c, "Expected one circuit body")
    modules = {o["attributes"]["sym_name"]: o for o in c["regions"][0]["operations"]
               if o["name"] in {"firrtl.module", "firrtl.extmodule", "firrtl.intmodule"}}
    top = top or c["attributes"]["name"]
    if top not in modules:
        reject(c, "Unknown top module " + top)
    seen = set()

    def visit_module(name):
        if name in seen:
            return
        seen.add(name)
        m = modules[name]
        if m["name"] != "firrtl.module":
            reject(m, "BlackBox/ExtModule/intrinsic requires an explicit semantics model: " + name)
        def visit(op):
            n = op["name"]
            if n == "firrtl.instance":
                target = op["attributes"]["moduleName"]["symbol"]
                if target not in modules:
                    reject(op, "Unknown instantiated module " + target)
                visit_module(target)
            if n.startswith("chirrtl.") or n in {"firrtl.mem", "firrtl.cmem", "firrtl.smem"}:
                reject(op, "Memory ports/latencies are not supported by the CIRCT frontend yet")
            if n in {"firrtl.assert", "firrtl.assume", "firrtl.cover", "firrtl.printf", "firrtl.stop"}:
                reject(op, "Unsupported verification or simulation operation " + n)
            if n == "firrtl.invalidvalue":
                # CIRCT may legally refine an invalid mux arm to its valid arm
                # during when expansion, even with --disable-opt. Until poison
                # conditions are carried through that lowering, reject the source.
                reject(op, "Explicit invalidvalue/DontCare requires definedness-preserving lowering")
            if "ref." in n or n in {"firrtl.attach", "firrtl.layerblock", "firrtl.force", "firrtl.release"}:
                reject(op, "Unsupported probe/layer/analog operation " + n)
            types = op["input_types"] + op["result_types"] + op["attributes"].get("portTypes", [])
            if any("asyncreset" in t for t in types):
                reject(op, "Asynchronous reset is unsupported")
            for region in op["regions"]:
                for child in region["operations"]:
                    visit(child)
        visit(m)
    visit_module(top)
    return top


def circt_tool(name):
    binary = name + (".exe" if os.name == "nt" else "")
    path = ROOT / ".tools" / f"firtool-{FIRTOOL_VERSION}" / f"firtool-{FIRTOOL_VERSION}" / "bin" / binary
    if not path.is_file():
        raise RuntimeError("Pinned CIRCT not installed; run python scripts/bootstrap.py --firtool")
    return path


def normalize_circt(source: Path, stage: Path, log: Path, top=None):
    import json
    header = re.match(r"\s*FIRRTL version (\d+)\.(\d+)\.(\d+)", source.read_text(encoding="utf-8-sig"))
    if not header or not (2, 0, 0) <= tuple(map(int, header.groups())) <= (6, 0, 0):
        raise ValueError(f"{source}:1:1: CIRCT adapter requires a supported FIRRTL version in 2.0.0..6.0.0")
    tool = circt_tool("firtool")
    flags = ["--mlir-print-op-generic", "--mlir-print-local-scope", "--mlir-print-debuginfo"]
    parsed = stage / "parsed.mlir"
    # Check unsupported effects before any optimizing transform can erase them.
    command = [str(tool), str(source), "--parse-only", *flags, "-o", str(parsed)]
    if code := run_logged(command, log / "circt-parse", echo=False):
        raise RuntimeError(f"Official CIRCT parse failed ({code}); see {log / 'circt-parse/stderr.log'}")
    top = preflight(GenericMLIR(parsed.read_text(encoding="utf-8"), str(source)).read(), top)
    lowered = stage / "lowered.mlir"
    command = [str(tool), str(source), "--ir-fir", "--preserve-aggregate=none", "--preserve-values=all",
               "--disable-opt", "--no-dedup", "--default-layer-specialization=enable", *flags, "-o", str(lowered)]
    if code := run_logged(command, log / "circt-lower", echo=False):
        raise RuntimeError(f"Official CIRCT lowering failed ({code}); see {log / 'circt-lower/stderr.log'}")
    root = GenericMLIR(lowered.read_text(encoding="utf-8"), str(source)).read()
    preflight(root, top)
    payload = stage / "circt.json"
    payload.write_text(json.dumps({"format": "chisel2lean-circt-ssa-v1", "circuit": circuit(root), "top": top}), encoding="utf-8")
    return payload, {"circt_version": FIRTOOL_VERSION, "circt_sha256": hashlib.sha256(tool.read_bytes()).hexdigest(),
                     "ssa_reader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     "bridge_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                     "parsed_mlir_sha256": hashlib.sha256(parsed.read_bytes()).hexdigest(),
                     "lowered_mlir_sha256": hashlib.sha256(lowered.read_bytes()).hexdigest(),
                     "unsupported_effects_checked_before_optimization": True}
