#!/usr/bin/env python3
"""Rebuild the one-ROM ablation from the same compiler control and model image."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    source, postpass, image, out = map(Path, sys.argv[1:])
    sys.path.insert(0, str(source))
    from scripts.pipeline.prepare_tinystories_1m_bram_image import staticize_sv
    from scripts.pipeline.fix_sv_synthesis_frontend import normalize
    from scripts.pipeline.validate_tinystories_stage import validate_stage

    image_manifest = json.loads((image / "manifest.json").read_text())
    assert image_manifest["status"] == "source-rebuilt-accepted-two-rom-static-image"
    records = image_manifest["source_memories"]
    assert len(records) == 89
    generated = (postpass / "control.sv").read_text()
    normalized, frontend = normalize(staticize_sv(generated, set(records)))
    out.mkdir()
    for name, record in records.items():
        filename = f"image-{name}.hex"
        data = (image / filename).read_bytes()
        assert sha256(data) == record["sha256"]
        shutil.copyfile(image / filename, out / filename)
    (out / "model-static-init-normalized.sv").write_text(normalized)
    (out / "manifest.json").write_text(json.dumps({
        "status": "source-rebuilt-one-rom-baseline-static-image",
        "source_memories": records,
        "image_records_sha256": image_manifest["image_records_sha256"],
        "generated_sv_sha256": sha256(generated.encode()),
        "normalized_sv_sha256": sha256(normalized.encode()),
        "frontend_normalization": frontend,
        "limits": ["One-ROM ablation baseline; no accepted route or board claim."],
    }, indent=2) + "\n")
    validate_stage("image", out, {})


if __name__ == "__main__":
    main()
