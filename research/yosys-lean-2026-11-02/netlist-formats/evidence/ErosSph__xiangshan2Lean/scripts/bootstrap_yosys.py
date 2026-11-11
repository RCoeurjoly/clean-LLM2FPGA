"""Install a hash-locked, project-local Yosys WASM distribution (Windows x64)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

from common import ROOT

PACKAGES = {
    "yowasp_yosys-0.70.0.0.post1259-py3-none-any.whl": "38a5ce6af95045b5b1ad795de7a73cfe88cf70c19f7b95f027f4a377a85310c0",
    "yowasp_runtime-1.96-py3-none-any.whl": "4ff456a4a6dff9d689c7feac9f68fb1492bed4cf873450d7b41259fa31645783",
    "wasmtime-47.0.1-py3-none-win_amd64.whl": "4e46a4a29092a2ac1001159d5673e2a23996f4474261c791401867b1630429f4",
    "platformdirs-4.13.0-py3-none-any.whl": "3dbcf4cd708f21cf876c4eaa90e58412bc4f033d87143f41b1493ff77c25b7e1",
    "click-8.5.0-py3-none-any.whl": "255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360",
}
INSTALL = ROOT / ".tools/yowasp-0.70"


def bootstrap():
    if sys.platform != "win32":
        raise RuntimeError("This wheel lock targets Windows x64; add a platform-specific lock before installing elsewhere")
    cache = ROOT / ".cache/yowasp-wheels"
    cache.mkdir(parents=True, exist_ok=True)
    for filename, digest in PACKAGES.items():
        wheel = cache / filename
        if not wheel.is_file():
            name, version = filename.split("-")[:2]
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as response:
                metadata = json.load(response)
            entry = next(x for x in metadata["urls"] if x["filename"] == filename)
            if entry["digests"]["sha256"] != digest:
                raise RuntimeError("PyPI hash differs from the committed lock: " + filename)
            urllib.request.urlretrieve(entry["url"], wheel)
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Wheel hash mismatch: " + str(wheel))
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                target = (INSTALL / info.filename).resolve()
                if not target.is_relative_to(INSTALL.resolve()):
                    raise RuntimeError("Wheel path escapes installation directory")
            archive.extractall(INSTALL)
    wasm = INSTALL / "yowasp_yosys/yosys.wasm"
    manifest = {"packages": PACKAGES, "yosys_version": "0.70", "upstream": "https://github.com/YosysHQ/yosys/tree/v0.70",
                "wasm_sha256": hashlib.sha256(wasm.read_bytes()).hexdigest()}
    (INSTALL / "chisel2lean-lock.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Installed project-local Yosys; " + manifest["wasm_sha256"])


if __name__ == "__main__":
    bootstrap()
