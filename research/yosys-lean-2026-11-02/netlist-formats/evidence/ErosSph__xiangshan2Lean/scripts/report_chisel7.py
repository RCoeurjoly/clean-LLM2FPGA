"""Aggregate fresh Chisel 7.3 evidence; earlier reports remain historical."""
import argparse
import json
from pathlib import Path
from common import ROOT, CHISEL_VERSION, SCALA_VERSION, FIRTOOL_VERSION
from differential import configurations, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated", type=Path, default=ROOT / "generated/chisel7")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/validation-chisel7.json")
    args = parser.parse_args()
    args.generated = args.generated.resolve()
    records, cases = [], []
    for suite in ["full", "edge", "control", "extension"]:
        path = ROOT / ("experiments/differential-v0.3-" + suite) / "summary.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        expected = {(n, json.dumps(p, sort_keys=True)) for n, p in configurations(suite)}
        actual = {(c["module"], json.dumps(c["parameters"], sort_keys=True)) for c in value["cases"]}
        fresh = all(c.get("fingerprint") == fingerprint(args.generated / "probes" / (c["module"] + ".ir.json"), c["parameters"]) for c in value["cases"])
        passed = expected == actual and fresh and value["failed"] == 0 and all(c["status"] == "PASS" for c in value["cases"])
        records.append({"path": path.relative_to(ROOT).as_posix(), "pass": passed, "fresh": fresh})
        cases.extend(value["cases"])
    paths = [ROOT / "experiments/rejections-v0.3/summary.json", ROOT / "experiments/mutation-v0.3/summary.json", ROOT / "experiments/client-proof-v0.3/result.json"]
    rejection, mutation, proof = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for path, passed in zip(paths, [rejection["failed"] == 0, mutation["status"] == "PASS", proof["exit_code"] == 0]):
        records.append({"path": path.relative_to(ROOT).as_posix(), "pass": passed})
    models = list((args.generated / "probes").glob("*.ir.json")) + [args.generated / "Counter.ir.json"]
    versions = all(json.loads(p.read_text(encoding="utf-8"))["chiselVersion"] == CHISEL_VERSION for p in models)
    report = {"status": "PASS" if versions and all(r["pass"] for r in records) else "FAIL", "version": "0.2.0",
              "chisel_version": CHISEL_VERSION, "scala_version": SCALA_VERSION, "firtool_version": FIRTOOL_VERSION,
              "source_to_lean_modules": len(models), "differential_configurations": len(cases),
              "differential_cycles": sum(c.get("cycles", 0) for c in cases),
              "compared_output_values": sum(c.get("compared_output_values", 0) for c in cases),
              "rejection_checks": rejection["passed"], "mutation_detected": mutation["status"] == "PASS",
              "client_model_theorems_checked": 2, "formal_equivalence_proof": False, "evidence": records}
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
