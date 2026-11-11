"""Fetch immutable, original XiangShan/CDE source dependencies into the local cache."""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

from common import ROOT

LOCK = ROOT / "verification" / "xiangshan" / "config-dependencies.json"
CACHE = ROOT / json.loads(LOCK.read_text(encoding="utf-8"))["cache_directory"]


def fetch_one(dependency):
    destination = CACHE / dependency["name"]
    marker = destination / ".chisel2lean-source.json"
    if marker.is_file():
        previous = json.loads(marker.read_text(encoding="utf-8"))
        if all(previous[k] == dependency[k] for k in ("repository", "commit")):
            return previous
        raise RuntimeError(f"Cached source revision differs: {destination}")
    url = f'https://codeload.github.com/{dependency["repository"]}/tar.gz/{dependency["commit"]}'
    with urllib.request.urlopen(url, timeout=180) as response:
        data = response.read()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(*Path(member.name).parts[1:])
            if not relative.parts:
                continue
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Archive path escapes source cache")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
            elif member.issym():
                # Build uses Scala source trees, not upstream helper symlinks.
                continue
            else:
                raise RuntimeError(f"Unsupported archive entry: {member.name}")
    record = {**dependency, "archive_sha256": hashlib.sha256(data).hexdigest(), "url": url}
    marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f'{dependency["name"]}: {dependency["commit"]}', flush=True)
    return record


def main():
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(fetch_one, lock["dependencies"]))
    (CACHE / "sources.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Pinned original sources ready: {CACHE}")


if __name__ == "__main__":
    main()
