#!/usr/bin/env python3
"""Compare native-cell counts for the committed one-/two-ROM builds."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys


METRICS = {
    "LUT1-6": ("LUT1", "LUT2", "LUT3", "LUT4", "LUT5", "LUT6"),
    "FF": ("FDRE", "FDCE", "FDPE", "FDSE"),
    "RAMB36E1": ("RAMB36E1",),
    "RAMB18E1": ("RAMB18E1",),
    "RAM64M": ("RAM64M",),
    "DSP48E1": ("DSP48E1",),
}
def hierarchical_counts(netlist: dict, top: str) -> Counter[str]:
    """Count instantiated primitives once per instance, not unused library modules."""
    modules = netlist["modules"]

    def visit(name: str, ancestors: frozenset[str]) -> Counter[str]:
        if name in ancestors:
            raise ValueError(f"recursive design hierarchy: {name}")
        counted: Counter[str] = Counter()
        for cell in modules[name]["cells"].values():
            kind = cell["type"]
            if kind in modules and modules[kind].get("attributes", {}).get("blackbox") != "00000000000000000000000000000001":
                counted.update(visit(kind, ancestors | {name}))
            else:
                counted[kind] += 1
        return counted

    return visit(top, frozenset())


def counts(path: Path) -> Counter[str]:
    netlist = json.loads((path / "design.json").read_text())
    return hierarchical_counts(netlist, "tinystories_1m_ypcb_bram_production_top")


def main() -> None:
    baseline, candidate, out = map(Path, sys.argv[1:])
    base_receipt = json.loads((baseline / "receipt.json").read_text())
    cand_receipt = json.loads((candidate / "receipt.json").read_text())
    assert base_receipt["status"] == "source-rebuilt-one-rom-baseline-synthesis"
    assert cand_receipt["status"] == "source-rebuilt-accepted-two-rom-synthesis"
    assert base_receipt["image_manifest"]["image_records_sha256"] == cand_receipt["image_manifest"]["image_records_sha256"]
    base, cand = counts(baseline), counts(candidate)
    measured = {
        metric: {"one_rom": sum(base[cell] for cell in cells),
                 "two_rom": sum(cand[cell] for cell in cells)}
        for metric, cells in METRICS.items()
    }
    for values in measured.values():
        values["delta"] = values["two_rom"] - values["one_rom"]
    out.mkdir()
    (out / "receipt.json").write_text(json.dumps({
        "status": "source-rebuilt-one-vs-two-rom-native-cell-comparison",
        "model_image_records_sha256": base_receipt["image_manifest"]["image_records_sha256"],
        "one_rom_structural_sha256": base_receipt["structural_sha256"],
        "two_rom_structural_sha256": cand_receipt["structural_sha256"],
        "measured": measured,
        "limits": ["Native Yosys cells only; one-ROM route and board operation not claimed."],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
