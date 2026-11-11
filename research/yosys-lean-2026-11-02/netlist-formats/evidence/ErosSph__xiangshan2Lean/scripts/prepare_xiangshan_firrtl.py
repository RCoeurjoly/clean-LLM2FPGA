"""Fetch the official XiangShan v2.1 source and exact recursive generator dependencies.

Downloads go only into .cache. No parameter/framework stubs are installed.
Build/run commands are separate so preparation cannot imply successful validation.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request
import zipfile

from common import ROOT

REVISION = "f0d8a1cf543c113433ff98140b6d77055d930deb"
WORK = ROOT / ".cache" / "xiangshan-v2.1"


def fetch(url):
    return urllib.request.urlopen(url, timeout=45).read()


def checkout(repo, revision, destination, records):
    marker = destination / ".snapshot.json"
    if marker.is_file():
        record = json.loads(marker.read_text())
        if record["revision"] != revision:
            raise RuntimeError("Existing snapshot has a different revision: " + str(destination))
    else:
        print(f"Downloading {repo}@{revision}", flush=True)
        payload = fetch(f"https://codeload.github.com/{repo}/zip/{revision}")
        archive = zipfile.ZipFile(io.BytesIO(payload))
        destination.mkdir(parents=True, exist_ok=True)
        for member in archive.infolist():
            parts = Path(member.filename).parts[1:]
            if not parts or member.is_dir():
                continue
            target = destination.joinpath(*parts).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe archive member")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
        record = {"repository": repo, "revision": revision, "archive_sha256": hashlib.sha256(payload).hexdigest()}
        marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    records.append(record | {"path": str(destination.relative_to(WORK))})
    modules = destination / ".gitmodules"
    if not modules.is_file():
        return
    text = modules.read_text()
    children = dict(re.findall(r"path\s*=\s*(\S+)\s+url\s*=\s*https://github.com/(\S+)", text))
    tree = json.loads(fetch(f"https://api.github.com/repos/{repo}/git/trees/{revision}"))
    for item in tree["tree"]:
        if item["type"] != "commit" or item["path"] not in children or item["path"] == "ready-to-run":
            continue
        child = children[item["path"]].removesuffix(".git")
        checkout(child, item["sha"], destination / item["path"], records)


if __name__ == "__main__":
    records = []
    checkout("OpenXiangShan/XiangShan", REVISION, WORK, records)
    (WORK / "dependency-lock.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    mill = ROOT / ".tools" / "mill-dist-0.9.8.jar"
    if not mill.is_file():
        print("Downloading Mill 0.9.8 (upstream .mill-version)", flush=True)
        mill.write_bytes(fetch("https://github.com/com-lihaoyi/mill/releases/download/0.9.8/0.9.8-assembly"))
    print(f"Prepared {len(records)} official source snapshots in {WORK}", flush=True)
