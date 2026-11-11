"""Run original XiangShan v2.1 with a small generation entry point, no framework shim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from common import ROOT, java_executable, run_logged
from prepare_xiangshan_firrtl import WORK, REVISION

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", choices=["RightShiftModule", "AluDataModule"], default="RightShiftModule")
    args = parser.parse_args()
    if not (WORK / "dependency-lock.json").is_file():
        raise RuntimeError("Run scripts/prepare_xiangshan_firrtl.py first")
    entry = ROOT / "verification" / "xiangshan" / "firrtl" / "GenerateModules.scala"
    target = WORK / "src" / "main" / "scala" / "validation" / entry.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(entry, target)
    evidence = ROOT / "experiments" / "xiangshan-firrtl" / args.module
    directory = evidence / "original"
    directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["COURSIER_CACHE"] = str(ROOT / ".cache" / "xiangshan-coursier")
    env["COURSIER_REPOSITORIES"] = "https://repo.maven.apache.org/maven2"
    env["chisel3Version"] = "3.5.3"
    env["chisel3-pluginVersion"] = "3.5.3"
    java = java_executable()
    env["JAVA_HOME"] = str(Path(java).parent.parent)
    command = [java, "-Xmx4g", "-XX:ActiveProcessorCount=4", "-Dfile.encoding=UTF-8",
               "-cp", str(ROOT / ".tools" / "mill-dist-0.9.8.jar"), "mill.MillMain", "-i", "XiangShan.runMain",
               "validation.GenerateModules", args.module, str(directory)]
    result = run_logged(command, evidence / "build-and-elaborate", cwd=WORK, env=env)
    if not result:
        provenance = {"repository": "OpenXiangShan/XiangShan", "commit": REVISION, "release": "v2.1",
                      "configuration": "top.MinimalConfig(1); bind XSCoreParamsKey to XSTileKey.head",
                      "framework_shim": False, "scala_version": "2.12.13", "chisel_version": "3.5.3", "mill_version": "0.9.8",
                      "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
                      "Alu.scala_sha256": hashlib.sha256((WORK / "src/main/scala/xiangshan/backend/fu/Alu.scala").read_bytes()).hexdigest(),
                      "dependency_lock": json.loads((WORK / "dependency-lock.json").read_text())}
        (evidence / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    raise SystemExit(result)
