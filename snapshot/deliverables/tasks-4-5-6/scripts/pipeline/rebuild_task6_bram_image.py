#!/usr/bin/env python3
"""Build the accepted full16 BRAM image from pinned model weights and SV."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    repo, postpass, package, snapshot, out = map(Path, sys.argv[1:])
    sys.path.insert(0, str(repo))
    from scripts.pipeline import run_bram_production_sv as runner
    from scripts.pipeline.prepare_tinystories_1m_bram_image import staticize_sv
    from scripts.pipeline.fix_sv_synthesis_frontend import normalize
    from TinyStories import capture_fixed_point_model_token_step_oracle as capture
    from scripts.pipeline.validate_tinystories_stage import validate_stage

    artifact = runner.production.generate_production_kernel(generation_steps=16)
    bundle = capture.exact_adapter.load_successor_exact_model(
        capture.DEFAULT_CONTRACT, package, snapshot, capture.DEFAULT_GENERATION)
    tensors = runner.base.materialize_model_sources(artifact, bundle)
    assert len(tensors) == 89, "unexpected immutable source-memory count"
    out.mkdir()
    records = {}
    for name, tensor in tensors.items():
        width = int(artifact.provenance["source_memories"][name]["width"])
        digits = (width + 3) // 4
        mask = (1 << width) - 1
        path = out / f"image-{name}.hex"
        path.write_text("".join(f"{int(value) & mask:0{digits}x}\n" for value in tensor.reshape(-1).tolist()))
        records[name] = {"width": width, "elements": tensor.numel(),
                         "bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    generated = (postpass / "model.sv").read_text()
    normalized, frontend = normalize(staticize_sv(generated, set(tensors)))
    (out / "model-static-init-normalized.sv").write_text(normalized)
    receipt = {
        "status": "source-rebuilt-accepted-two-rom-static-image",
        "generation_steps": 16,
        "source_memories": records,
        "image_records_sha256": sha256(canonical),
        "generated_sv_sha256": sha256(generated.encode()),
        "normalized_sv_sha256": sha256(normalized.encode()),
        "frontend_normalization": frontend,
        "limits": ["Image and staticized SV only; synthesis, route, bitstream and board not rebuilt."],
    }
    (out / "manifest.json").write_text(json.dumps(receipt, indent=2) + "\n")
    validate_stage("image", out, {"postpass": postpass})


if __name__ == "__main__":
    main()
