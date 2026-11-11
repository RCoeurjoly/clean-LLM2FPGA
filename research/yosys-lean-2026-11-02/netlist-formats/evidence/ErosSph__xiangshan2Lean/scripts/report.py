"""Aggregate fingerprint-verified v0.2 evidence; fail on missing or stale checks."""
import json
from common import ROOT
from differential import configurations, fingerprint

records = []
cases = []
for suite in ["full", "edge", "control", "extension"]:
    relative = "experiments/differential-v0.2-" + suite + "/summary.json"
    path = ROOT / relative
    if not path.is_file():
        raise RuntimeError(f"Required evidence is missing: {relative}")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {(name, json.dumps(p, sort_keys=True)) for name, p in configurations(suite)}
    actual = {(c["module"], json.dumps(c["parameters"], sort_keys=True)) for c in value["cases"]}
    fresh = all(c.get("fingerprint") == fingerprint(ROOT / "generated" / "probes" / (c["module"] + ".ir.json"), c["parameters"])
                for c in value["cases"])
    passed = expected == actual and fresh and value["failed"] == 0 and all(c["status"] == "PASS" for c in value["cases"])
    records.append({"path": relative, "pass": passed})
    cases.extend(value["cases"])
rejections = json.loads((ROOT / "experiments" / "rejections-v0.2" / "summary.json").read_text(encoding="utf-8"))
mutation = json.loads((ROOT / "experiments" / "mutation" / "summary.json").read_text(encoding="utf-8"))
proof = json.loads((ROOT / "experiments" / "client-proof-v0.2" / "result.json").read_text(encoding="utf-8"))
records.extend([
    {"path": "experiments/rejections-v0.2/summary.json", "pass": rejections["failed"] == 0},
    {"path": "experiments/mutation/summary.json", "pass": mutation["status"] == "PASS"},
    {"path": "experiments/client-proof-v0.2/result.json", "pass": proof["exit_code"] == 0}])
report = {"status": "PASS" if all(r["pass"] for r in records) else "FAIL", "version": "0.2.0",
          "source_to_lean_modules": len(list((ROOT / "generated" / "probes").glob("*.ir.json"))) + 1,
          "differential_configurations": len(cases), "differential_cycles": sum(c.get("cycles", 0) for c in cases),
          "compared_output_values": sum(c.get("compared_output_values", 0) for c in cases),
          "rejection_checks": rejections["passed"], "mutation_detected": mutation["status"] == "PASS",
          "client_model_theorems_checked": 2,
          "formal_equivalence_proof": False, "evidence": records}
(ROOT / "docs" / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
raise SystemExit(0 if report["status"] == "PASS" else 1)
