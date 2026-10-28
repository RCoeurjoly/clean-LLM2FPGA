#!/usr/bin/env python3
"""Combine real Nix family-stage receipts without inventing fit or timing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_matrix(inventory: list[dict], receipts: dict[str, dict],
                 identities: dict[str, dict[str, dict[str, str]]] | None = None,
                 archived_synthesis: dict[str, dict] | None = None,
                 board_capacity: dict | None = None) -> dict:
    bram36_available = None
    if board_capacity is not None:
        bram36_available = board_capacity.get("resources", {}).get("available", {}).get("RAMB36E1")
        if type(bram36_available) is not int or bram36_available <= 0:
            raise ValueError("board RAMB36E1 capacity missing or invalid")
    models = []
    for model in inventory:
        size, revision = model["size_label"], model["revision"]
        record = receipts[size]
        compiler, image = record["compiler"], record["image"]
        for label, stage in (("compiler", compiler), ("image", image)):
            if stage.get("size_label") != size or stage.get("revision") != revision:
                raise ValueError(f"{size}: {label} size/revision differs from inventory")
        memories = compiler.get("source_memories")
        if not isinstance(memories, dict) or not memories or image.get("source_memories") != memories:
            raise ValueError(f"{size}: compiler/image source memories differ")
        for name, entry in memories.items():
            if (not isinstance(entry, dict) or any(
                    type(entry.get(dimension)) is not int or entry[dimension] <= 0
                    for dimension in ('width', 'elements'))):
                raise ValueError(f"{size}: invalid memory dimensions for {name}")
        image_bits = sum(entry["width"] * entry["elements"] for entry in memories.values())
        if image_bits <= 0:
            raise ValueError(f"{size}: nonpositive source image size")
        simulation = record["simulation"]
        if (simulation.get("status") != "matched"
                or type(simulation.get("first_token")) is not int
                or type(simulation.get("cycles")) is not int
                or simulation["cycles"] <= 0):
            raise ValueError(f"{size}: first-token simulation did not match")
        archived = (archived_synthesis or {}).get(size)
        if archived is not None:
            if "synthesis" in record or (identities is not None and
                                           "synthesis" in identities[size]):
                raise ValueError(f"{size}: archived synthesis has a current result")
            if (archived.get("origin") != "archived-donor"
                    or archived.get("status") not in {"failed", "timeout"}
                    or archived.get("resources") is not None
                    or not archived.get("donor_commit")
                    or not archived.get("reason")):
                raise ValueError(f"{size}: malformed archived synthesis failure")
            synthesis = archived
        else:
            synthesis = record["synthesis"]
        synth_status = synthesis.get("status")
        if synth_status not in {"pass", "timeout", "failed"}:
            raise ValueError(f"{size}: unknown synthesis status")
        resources = synthesis.get("resources")
        if synth_status == "pass":
            if not isinstance(resources, dict) or not all(
                type(resources.get(name)) is int and resources[name] >= 0
                for name in ("LUT", "FF", "BRAM36", "BRAM18", "DSP")
            ):
                raise ValueError(f"{size}: passing synthesis lacks mapped resources")
        elif resources is not None:
            raise ValueError(f"{size}: failed synthesis cannot claim resources")
        row = {
            "size_label": size, "revision": revision,
            "parameters": model["parameters"],
            "n_layer": model["n_layer"], "n_embd": model["n_embd"],
            "image_bits": image_bits,
            "stages": {
                "compiler_sv": {"status": "pass", "sv_bytes": compiler["sv_bytes"]},
                "image": {"status": "pass"},
                "simulation": {"status": "pass", "first_token": simulation["first_token"],
                               "cycles": simulation["cycles"]},
                "synthesis": {"status": synth_status, "reason": synthesis.get("reason"),
                              "origin": "archived-donor" if archived is not None else "current-nix"},
                "route": {"status": "not_attempted" if synth_status == "pass" else "skipped"},
                "board": {"status": "not_attempted" if synth_status == "pass" else "skipped"},
            },
            "resources": resources,
            "fmax_mhz": None,
            "first_failure": (None if synth_status == "pass" else
                              "synthesis: " + str(synthesis.get("reason") or synth_status)),
        }
        if bram36_available is not None:
            # Two 18-Kibit primitives consume one 36-Kibit block equivalent.
            # This necessary capacity screen does not prove placement or fit.
            required = (resources["BRAM36"] + resources["BRAM18"] / 2
                        if resources is not None else None)
            conclusion = ("unavailable" if required is None else
                          "exceeds_bram36_capacity" if required > bram36_available else
                          "not_disqualified_by_bram36")
            row["capacity_screen"] = {
                "bram36_required": required, "bram36_available": bram36_available,
                "conclusion": conclusion,
            }
        if archived is not None:
            row["stages"]["synthesis"]["donor_commit"] = archived["donor_commit"]
        if identities is not None:
            stages = [("compiler", "compiler_sv"), ("image", "image"),
                      ("simulation", "simulation")]
            if archived is None:
                stages.append(("synthesis", "synthesis"))
            for source, destination in stages:
                identity = identities[size][source]
                if (not isinstance(identity.get("output"), str)
                        or not identity["output"]
                        or not isinstance(identity.get("derivation"), str)
                        or not identity["derivation"].endswith(".drv")):
                    raise ValueError(f"{size}: {source} Nix identity missing")
                row["stages"][destination].update(identity)
        models.append(row)
    return {
        "schema": "task5-tinystories-family-stage-matrix-v1",
        "scope": "generated SV, exact first-token RTL and bounded XC7 synthesis; no route or board claim",
        "models": models,
    }


def render_report(matrix: dict) -> str:
    lines = [
        "# TinyStories family compiler scaling gates", "",
        "All rows use one fixed-point Calyx generator and pinned model-specific oracles.",
        "A synthesis pass is not a board-fit or timing result.", "",
        "| Size | Parameters | Source image (MiB) | RTL first token / cycles | Synthesis origin | XC7 cells (LUT/FF/BRAM36/BRAM18/DSP) | BRAM36-equivalent capacity screen | First failure |",
        "| --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in matrix["models"]:
        resources = row["resources"]
        cells = ("/".join(str(resources[name]) for name in
                          ("LUT", "FF", "BRAM36", "BRAM18", "DSP"))
                 if resources is not None else "unavailable")
        sim = row["stages"]["simulation"]
        lines.append(
            f"| {row['size_label']} | {row['parameters']:,} | "
            f"{row['image_bits'] / 8 / 1048576:.2f} | "
            f"{sim['first_token']} / {sim['cycles']:,} | "
            f"{row['stages']['synthesis']['origin']} | {cells} | "
            f"{row.get('capacity_screen', {}).get('conclusion', 'unmeasured')} | "
            f"{row['first_failure'] or 'none at synthesis gate'} |"
        )
    lines += ["", "Fmax, fit, route and YPCB inference are unmeasured for these family artifacts.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    parser.add_argument("--archived-synthesis", type=Path)
    parser.add_argument("--board-capacity", type=Path)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    paths = json.loads(args.inputs.read_text())
    archived = (json.loads(args.archived_synthesis.read_text())
                if args.archived_synthesis else {})
    capacity = (json.loads(args.board_capacity.read_text())
                if args.board_capacity else None)
    receipts = {}
    for size, record in paths.items():
        receipts[size] = {
            "compiler": json.loads((Path(record["compiler"]) / "build.json").read_text()),
            "image": json.loads((Path(record["image"]) / "image.json").read_text()),
            "simulation": json.loads((Path(record["simulation"]) / "result.json").read_text()),
        }
        if size not in archived:
            receipts[size]["synthesis"] = json.loads(
                (Path(record["synthesis"]) / "result.json").read_text())
    identities = {
        size: {
            stage: {"output": record[stage], "derivation": record[f"{stage}_drv"]}
            for stage in (("compiler", "image", "simulation") if size in archived
                          else ("compiler", "image", "simulation", "synthesis"))
        }
        for size, record in paths.items()
    }
    matrix = build_matrix(inventory, receipts, identities,
                          archived_synthesis=archived, board_capacity=capacity)
    args.output.write_text(json.dumps(matrix, sort_keys=True, indent=2) + "\n")
    args.markdown.write_text(render_report(matrix))


if __name__ == "__main__":
    main()
