#!/usr/bin/env python3
"""Materialize the production model image for synthesis-time BRAM init.

The normal Verilator harness preloads authenticated tensors from host files.
That is not a board image: synthesis ignores the dynamic DATA plusarg used by
the generated SV.  This tool emits width-correct hex files and a staticized SV
copy whose immutable source memories use those files through ``$readmemh``.
Mutable scratch memories are deliberately not initialized here; reset and the
compiler schedule own them.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staticize_sv(source: str, source_names: set[str]) -> str:
    """Make the generated initial block synthesis-visible and source-only."""
    source = re.sub(r"^string DATA;\nint CODE;\n", "", source, count=1, flags=re.M)
    source = re.sub(r"^\s*CODE = \$value\$plusargs\([^\n]*\n", "", source, count=1, flags=re.M)
    source = re.sub(r"^\s*\$display\(\"DATA[^\n]*\n", "", source, count=1, flags=re.M)
    # Calyx emits simulation-only onehot/fatal guards.  The synthesis frontend
    # used for the board image does not implement these system tasks.
    source = re.sub(
        r"always_comb begin\n\s*if\(~\$onehot0\(\{[^}]+\}\)\) begin\n\s*\$fatal\([^;]+\);\n\s*end\nend\n",
        "",
        source,
    )

    # The Calyx-generated preload block targets hierarchical instance members
    # (``instance.mem``).  Yosys does not accept such a target as a synthesis
    # memory.  Move initialization into each memory module and select the file
    # through an instance parameter instead.
    source = re.sub(
        r"\ninitial begin\n(?:\s*\$readmemh\([^\n]+\);\n)+end\n",
        "\n",
        source,
    )

    source = re.sub(
        r"^\s*\$readmemh\(\{DATA, \"/(\w+)\.dat\"\}, \1\.mem\);$",
        lambda match: "",
        source,
        flags=re.M,
    )
    # The generated final block writes every memory back to host files.  It is
    # simulation-only and must not survive into synthesis.
    source = re.sub(r"\nfinal begin\n.*?\nend\n(?=logic )", "\n", source, count=1, flags=re.S)
    # Add an optional image parameter and a local (synthesis-visible)
    # initializer to every generated memory implementation.
    source = source.replace(
        "parameter IDX_SIZE = 4\n)",
        'parameter IDX_SIZE = 4,\n    parameter INIT_FILE = ""\n)',
    )
    source = source.replace(
        "logic [WIDTH-1:0] mem[SIZE-1:0];",
        'logic [WIDTH-1:0] mem[SIZE-1:0];\n\n  initial if (INIT_FILE != "") $readmemh(INIT_FILE, mem);',
    )
    for name in sorted(source_names):
        init_parameter = f'.INIT_FILE("image-{name}.hex")'
        existing = source.count(init_parameter)
        if existing == 1:
            continue
        if existing > 1:
            raise ValueError(f"immutable memory must have exactly one initializer: {name}")
        instance = re.search(r"\b" + re.escape(name) + r"\s*\(", source)
        if instance is None:
            raise ValueError(f"missing immutable memory instance: {name}")
        instance_start = instance.start()
        type_match = list(re.finditer(r"\bseq_mem_d1(?:_bram|_lutram)?\b", source[:instance_start]))
        if not type_match or instance_start - type_match[-1].end() > 256:
            raise ValueError(f"missing immutable memory instance: {name}")
        type_end = type_match[-1].end()
        hash_open = source.find("# (", type_end, instance_start)
        if hash_open >= 0:
            close = source.rfind(")", hash_open, instance_start)
            if close < 0:
                raise ValueError(f"malformed immutable memory instance: {name}")
            source = source[:close] + ',\n  .INIT_FILE("image-' + name + '.hex")' + source[close:]
        else:
            source = source[:type_end] + ' #(.INIT_FILE("image-' + name + '.hex"))' + source[type_end:]
    for name in source_names:
        if source.count(f'.INIT_FILE("image-{name}.hex")') != 1:
            raise ValueError(f"immutable memory must have exactly one initializer: {name}")
    return source


def materialize(output: Path, *, sv: Path | None = None, generation_steps: int = 2) -> dict[str, object]:
    production = load(ROOT / "scripts/pipeline/lower_fixed_point_model_bram_production.py", "bram_image_production")
    capture = load(ROOT / "TinyStories/capture_fixed_point_model_token_step_oracle.py", "bram_image_capture")
    artifact = production.generate_production_kernel(generation_steps=generation_steps)
    bundle = capture.exact_adapter.load_successor_exact_model(
        capture.DEFAULT_CONTRACT, capture.DEFAULT_PACKAGE,
        capture.DEFAULT_MODEL, capture.DEFAULT_GENERATION,
    )
    tensors = production.base.materialize_model_sources(artifact, bundle)
    output.mkdir(parents=True, exist_ok=True)
    records: dict[str, object] = {}
    for name, tensor in tensors.items():
        width = int(artifact.provenance["source_memories"][name]["width"])
        mask = (1 << width) - 1
        digits = max(1, (width + 3) // 4)
        path = output / f"image-{name}.hex"
        values = tensor.reshape(-1).tolist()
        path.write_text("".join(f"{int(value) & mask:0{digits}x}\n" for value in values))
        records[name] = {
            "width": width,
            "elements": len(values),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
    if sv is None:
        raise ValueError("sv_required")
    original = sv.read_text()
    static = staticize_sv(original, set(tensors))
    static_path = output / "model-static-init.sv"
    static_path.write_text(static)
    manifest: dict[str, object] = {
        "schema": "tinystories-1m-bram-static-image-v1",
        "generation_steps": generation_steps,
        "oracle_sha256": artifact.provenance["oracle_sha256"],
        "futil_sha256": artifact.provenance["futil_sha256"],
        "source_memories": records,
        "generated_sv_sha256": sha256(sv),
        "static_sv_sha256": sha256(static_path),
        "static_sv": str(static_path),
        "claims": {"synthesis_image_ready": True, "bitstream_verified": False},
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sv", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--generation-steps", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    print(json.dumps(materialize(args.output.resolve(), sv=args.sv.resolve(), generation_steps=args.generation_steps), indent=2))


if __name__ == "__main__":
    main()
