"""Map used real CDE configuration fields to generated Lean Params; optionally emit original RTL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import ROOT, java_executable, rtl_environment, run_logged


def materialize(ir_path: Path, factories: dict, constructor: dict, output: Path,
                classpath_file: Path, evidence: Path, rtl_dir: Path | None = None):
    evidence.mkdir(parents=True, exist_ok=True)
    factory_path, constructor_path = evidence / "factories.json", evidence / "constructor.json"
    factory_path.write_text(json.dumps(factories, indent=2), encoding="utf-8")
    constructor_path.write_text(json.dumps(constructor, indent=2), encoding="utf-8")
    command = [java_executable(), "-Xmx4g", "-Dfile.encoding=UTF-8", "-cp",
               classpath_file.read_text(encoding="utf-8").strip(), "reference.ConfiguredModule",
               str(ir_path.resolve()), str(factory_path.resolve()), str(constructor_path.resolve()), str(output.resolve())]
    if rtl_dir:
        command.append(str(rtl_dir.resolve()))
    code = run_logged(command, evidence / "execution", env=rtl_environment())
    if code:
        raise RuntimeError(f"Real configuration materialization failed; see {evidence}")
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    from simulate import eval_param
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    if set(snapshot["parameters"]) != {p["name"] for p in ir["parameters"]}:
        raise RuntimeError("Materialized fields differ from the generated parameter schema")
    if not all(eval_param(r, snapshot["parameters"]) for r in ir["requirements"]):
        raise ValueError("Real configuration violates the source/model legal domain")
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ir", type=Path)
    parser.add_argument("--factories", required=True, type=Path, help='JSON: {"p": {"class": "package.ConfigClass", "args": []}}')
    parser.add_argument("--constructor", type=Path, help="JSON containing primitive constructor parameters")
    parser.add_argument("--classpath-file", default=ROOT / ".cache" / "reference-classpath.txt", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rtl-dir", type=Path)
    parser.add_argument("--evidence", default=ROOT / "experiments" / "configuration", type=Path)
    args = parser.parse_args()
    materialize(args.ir, json.loads(args.factories.read_text(encoding="utf-8")),
                json.loads(args.constructor.read_text(encoding="utf-8")) if args.constructor else {},
                args.out, args.classpath_file, args.evidence, args.rtl_dir)
    print(f"Lean parameter snapshot: {args.out}")


if __name__ == "__main__":
    main()
