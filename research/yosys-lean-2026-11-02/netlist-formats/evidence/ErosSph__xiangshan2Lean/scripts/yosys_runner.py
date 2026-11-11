"""Run the locked Yosys, mounting only the subprocess working directory."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

from bootstrap_yosys import INSTALL
from common import ROOT


def main():
    if sys.argv[1:2] == ['--native']:
        from bootstrap_native_yosys import environment, executable, load_lock
        load_lock()
        return subprocess.call([str(executable()), *sys.argv[2:]], env=environment())
    lock_path = INSTALL / "chisel2lean-lock.json"
    if not lock_path.is_file():
        raise RuntimeError("Run python scripts/bootstrap_yosys.py first")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if hashlib.sha256((INSTALL / "yowasp_yosys/yosys.wasm").read_bytes()).hexdigest() != lock["wasm_sha256"]:
        raise RuntimeError("Yosys WASM does not match the local lock")
    sys.path.insert(0, str(INSTALL))
    temp = ROOT / ".cache/yowasp-temp"
    temp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = str(temp)
    os.environ["YOWASP_CACHE_DIR"] = str(ROOT / ".cache/yowasp-runtime")
    os.environ["YOWASP_MOUNT"] = "/work=."
    import yowasp_yosys
    return yowasp_yosys.run_yosys(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
