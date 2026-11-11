"""Convert elaborated FIRRTL/CHIRRTL to a checked, fixed-configuration Lean model.

This entry point reuses the typed IR/backend, without the Chicala Scala frontend.
Register reset signals remain explicit inputs. No power-on state is invented.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

from chisel2lean4._compat import enable_legacy_script_imports

enable_legacy_script_imports()
from chisel2lean import check_lean
from common import ROOT, java_executable, run_logged, sbt_command


def translate_firrtl(source: Path, output: Path, top: str | None = None,
                     namespace: str | None = None, check: bool = True,
                     rebuild: bool = False, frontend: str = "auto", evidence: Path | None = None) -> int:
    source, output = source.resolve(), output.resolve()
    if frontend not in {"auto", "classic", "circt"}:
        raise ValueError("Unknown FIRRTL frontend " + frontend)
    header = re.match(r"\s*FIRRTL version (\d+)\.(\d+)\.(\d+)", source.read_text(encoding="utf-8-sig"))
    version = header[0].strip().split()[-1] if header else None
    modern = frontend == "circt" or (frontend == "auto" and header and int(header[1]) >= 2)
    attempt = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    log = (evidence.resolve() if evidence else ROOT / "experiments" / "firrtl-translation") / attempt
    stage = log / "artifacts"
    stage.mkdir(parents=True)
    classpath_file = ROOT / ".cache" / "firrtl-classpath.txt"
    if rebuild or not classpath_file.is_file():
        command, env = sbt_command(["classicFirrtl/writeFirrtlClasspath"])
        if code := run_logged(command, log / "build", env=env):
            return code
    classpath = classpath_file.read_text(encoding="utf-8").strip()
    compiler = Path(classpath.split(";" if sys.platform == "win32" else ":")[0])
    circt_metadata = {}
    if modern:
        from circt_frontend import normalize_circt
        try:
            payload, circt_metadata = normalize_circt(source, stage, log, top)
        except (RuntimeError, ValueError) as error:
            (log / "diagnostic.txt").write_text(str(error), encoding="utf-8")
            print(str(error), file=sys.stderr)
            return 2
        selected_top = json.loads(payload.read_text(encoding="utf-8"))["top"]
        command = [java_executable(), "-Xmx2g", "-Dfile.encoding=UTF-8", "-cp", classpath,
                   "chicala.lean.firrtl.CirctMain", str(payload), str(stage), namespace or "FIRRTL." + selected_top, str(source)]
    else:
        command = [java_executable(), "-Xmx2g", "-Dfile.encoding=UTF-8", "-cp", classpath,
                   "chicala.lean.firrtl.FirrtlMain", str(source), str(stage), top or "", namespace or ""]
    if code := run_logged(command, log / "compiler"):
        print(f"FIRRTL translation failed; evidence: {log}", file=sys.stderr)
        return code
    runtime = stage / "Chisel2Lean" / "Runtime.lean"
    runtime.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "lean" / "Chisel2Lean" / "Runtime.lean", runtime)
    if check and (code := check_lean(stage, log / "lean")):
        print(f"Lean check failed; evidence: {log}", file=sys.stderr)
        return code
    ir_files = list(stage.rglob("*.ir.json"))
    if len(ir_files) != 1:
        raise RuntimeError("FIRRTL frontend must produce exactly one selected circuit model")
    ir = json.loads(ir_files[0].read_text(encoding="utf-8"))
    manifest = {"format": "chisel2lean-firrtl-manifest-v1", "frontend": "firrtl",
                "frontend_implementation": "CIRCT" if modern else "Scala FIRRTL",
                "firrtl_version": version if modern else "1.5.6", "source_firrtl_version": version,
                "lean_version": "4.19.0", "source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "compiler_sha256": hashlib.sha256(compiler.read_bytes()).hexdigest(),
                "configuration": "already-elaborated", "symbolic_parameters": False,
                "modules": [{"module": ir["module"], "top": ir["circuitTop"],
                             "ir": ir_files[0].relative_to(stage).as_posix(),
                             "lean": ir_files[0].relative_to(stage).as_posix().replace(".ir.json", ".lean")}],
                "lean_checked": check, "evidence": str(log), **circt_metadata}
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for path in stage.rglob("*"):
        if path.is_file() and path.suffix in {".lean", ".olean", ".json", ".fir", ".mlir"}:
            destination = output / path.relative_to(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    print(f"Generated fixed-configuration Lean model: {ir['module']}\nEvidence: {log}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "generated" / "firrtl")
    parser.add_argument("--top", help="module to select as circuit top (default: circuit main)")
    parser.add_argument("--namespace", help="Lean namespace (default: FIRRTL.<top>)")
    parser.add_argument("--check", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--frontend", choices=["auto", "classic", "circt"], default="auto",
                        help="auto selects pinned CIRCT for FIRRTL >= 2; classic keeps the old reader")
    parser.add_argument("--evidence", type=Path, help="Directory for isolated translation attempts and diagnostics")
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"FIRRTL file not found: {args.source}")
    return translate_firrtl(args.source, args.out, args.top, args.namespace, args.check, args.rebuild, args.frontend, args.evidence)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
