"""Package source, generated examples and current validation reports without caches."""
import hashlib
import json
from pathlib import Path
import zipfile
import subprocess
import sys
from common import ROOT

subprocess.run([sys.executable, str(ROOT / "scripts" / "report_chisel7.py")], cwd=ROOT, check=True)
report = json.loads((ROOT / "docs" / "validation-chisel7.json").read_text(encoding="utf-8"))
if report.get("status") != "PASS" or report.get("version") != "0.2.0":
    raise SystemExit("Run the complete Chisel 7.3 validation before packaging")
models = []
for path in sorted((ROOT / "generated").rglob("*.ir.json")):
    value = json.loads(path.read_text(encoding="utf-8"))
    lean = path.with_suffix("").with_suffix(".lean")
    models.append({"module": value["module"], "namespace": value["namespace"],
                   "ir": path.relative_to(ROOT / "generated").as_posix(),
                   "lean": lean.relative_to(ROOT / "generated").as_posix(),
                   "lean_sha256": hashlib.sha256(lean.read_bytes()).hexdigest(),
                   "ir_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
inventory = {"format": "chisel2lean-release-manifest-v2", "version": "0.2.0", "modules": models,
             "lean_version": "4.19.0", "chisel_version": "7.3.0", "validation": report}
(ROOT / "generated" / "release-manifest.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
paths = set()
for name in ["src", "scripts", "lean", "audit/src", "reference/src", "testcase/src", "tests", "examples", "docs", "generated", "vendor", "verification"]:
    directory = ROOT / name
    paths.update(p for p in directory.rglob("*") if p.is_file()
                 and not any(part in {"__pycache__", "target", ".cache", ".tools"} for part in p.parts)
                 and p.suffix not in {".pyc", ".olean", ".ilean"})
paths.update(ROOT / name for name in ["README.md", "LICENSE", "NOTICE", "AGENTS.md", "TASKS.md", "build.sbt", "lean-toolchain", "chisel2lean4.ps1", ".gitignore", "project/build.properties", "project/repositories"])
for evidence in report["evidence"]:
    path = ROOT / evidence["path"]
    paths.add(path)
    if path.name == "summary.json" and "differential" in str(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data["cases"]:
            directory = Path(case["evidence"])
            if not directory.is_absolute():
                directory = ROOT / directory
            paths.update(p for p in directory.rglob("*") if p.is_file()
                         and "obj_dir" not in p.parts and p.suffix not in {".olean", ".ilean", ".exe", ".o", ".a"})
for directory in ["experiments/client-proof-v0.2", "experiments/build/translator-final-v0.2", "experiments/build/initializer-alias-v0.2", "experiments/rejections-v0.2"]:
    paths.update(p for p in (ROOT / directory).rglob("*") if p.is_file()
                 and not any(part in {"artifacts", "generated", "classes"} for part in p.parts)
                 and p.suffix not in {".olean", ".ilean", ".class"})
checksums = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file()}
manifest = {"version": "0.2.0", "upstream_commit": "c8bab84b24c448c611b8bf2a87c7e9c26014b081",
            "toolchain": {"chisel": "7.3.0", "scala": "2.13.15", "sbt": "1.8.2", "lean": "4.19.0"},
            "files_sha256": checksums, "validation": report}
destination = ROOT / "dist" / "chisel2lean4-chisel7.3.zip"
destination.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
    for name in checksums:
        output.write(ROOT / name, "chisel2lean4-0.2.0/" + name)
    output.writestr("chisel2lean4-0.2.0/RELEASE.json", json.dumps(manifest, indent=2))
with zipfile.ZipFile(destination) as packaged:
    if packaged.testzip() is not None:
        raise SystemExit("Archive verification failed")
    for name, expected in checksums.items():
        if hashlib.sha256(packaged.read("chisel2lean4-0.2.0/" + name)).hexdigest() != expected:
            raise SystemExit("Packaged file checksum mismatch: " + name)
checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
destination.with_suffix(".sha256").write_text(checksum + "  " + destination.name + "\n", encoding="utf-8")
print(json.dumps({"archive": str(destination), "files": len(checksums), "bytes": destination.stat().st_size, "sha256": checksum}, indent=2))
