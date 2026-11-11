"""Compare generated Lean against independently generated and simulated RTL."""
from __future__ import annotations

import argparse
import itertools
import json
import hashlib
from pathlib import Path
import random
import sys
import time
from common import ROOT
from simulate import eval_param, simulate
from rtl import run_rtl


def flat_outputs(values, signals=()):
    output = {}
    layouts = {s["name"]: s["source_path"] for s in signals if "source_path" in s}
    def visit(name, value, indices=()):
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(name, item, (*indices, index))
        else:
            if name in layouts:
                positions = iter(indices)
                path = ".".join(str(next(positions)) if part == "*" else part for part in layouts[name])
            else:
                path = ".".join([name, *map(str, indices)])
            output[path] = int(value)
    for name, value in values.items():
        visit(name, value)
    return output


def random_value(tpe, parameters, rng):
    if tpe["kind"] == "bool":
        return bool(rng.randrange(2))
    if tpe["kind"] == "bits":
        return rng.randrange(1 << max(0, eval_param(tpe["width"], parameters)))
    return [random_value(tpe["element"], parameters, rng) for _ in range(eval_param(tpe["size"], parameters))]


def initial_state(ir, parameters):
    def zero(t):
        if t["kind"] == "bool":
            return False
        if t["kind"] == "bits":
            return 0
        return [zero(t["element"]) for _ in range(eval_param(t["size"], parameters))]
    regs = [s for s in ir["signals"] if s["kind"] == "reg"]
    return None if len(ir["resets"]) == len(regs) else {s["name"]: zero(s["type"]) for s in regs}


def make_trace(ir, parameters, seed=20261013, cycles=48):
    inputs = [s for s in ir["signals"] if s["kind"] == "input"]
    regs = [s for s in ir["signals"] if s["kind"] == "reg"]
    rng = random.Random(seed)
    # Exhaust all input combinations for small scalar combinational examples.
    domains = []
    bits = 0
    for signal in inputs:
        t = signal["type"]
        if t["kind"] == "vec":
            domains = []
            break
        width = 1 if t["kind"] == "bool" else eval_param(t["width"], parameters)
        bits += width
        if bits > 7:
            domains = []
            break
        domains.append([False, True] if t["kind"] == "bool" else range(1 << width))
    exhaustive = not regs and len(domains) == len(inputs)
    trace = []
    for cycle in range(cycles):
        values = {s["name"]: random_value(s["type"], parameters, rng) for s in inputs}
        trace.append({"reset": bool(regs) and cycle in {0, cycles // 2}, "inputs": values})
    if exhaustive:
        trace = [{"inputs": dict(zip([s["name"] for s in inputs], values))} for values in itertools.product(*domains)]
    name = ir["module"].split(".")[-1]
    if name in {"Counter", "DirectCounter", "RenamedAccumulator"}:
        active = inputs[0]["name"]
        width = parameters.get("n", parameters.get("width"))
        trace = [{"reset": True, "inputs": {active: True}}]
        trace += [{"inputs": {active: True}} for _ in range((1 << width) + 2)]
        trace += [{"inputs": {active: False}}, {"reset": True, "inputs": {active: True}}]
    if name == "EnabledRegister":
        trace[0]["inputs"]["en"] = True
    if name in {"MoreBits", "SignedDivision"}:
        trace = [entry for entry in trace if entry["inputs"]["b"] != 0]
    if name in {"RegisterFile", "AsyncMemory", "SyncMemory"}:
        n = parameters["n"]
        mask = (1 << (n - 1).bit_length()) - 1
        for entry in trace:
            for port in ["waddr", "raddr"]:
                if (entry["inputs"][port] & mask) >= n:
                    entry["inputs"][port] = rng.randrange(n)
            if name == "SyncMemory":
                entry["inputs"]["readEnable"] = True
                if entry["inputs"]["write"] and (entry["inputs"]["waddr"] & mask) == (entry["inputs"]["raddr"] & mask):
                    entry["inputs"]["write"] = False
    if name == "Fifo":
        n = parameters["depth"]
        def entry(enq, deq, data=0, reset=False):
            return {"reset": reset, "inputs": {"enq": enq, "deq": deq, "data": data}}
        directed = [entry(False, False, reset=True)]
        directed += [entry(True, False, k % (1 << parameters["w"])) for k in range(n + 2)]
        directed += [entry(True, True, 1) for _ in range(n + 2)]
        directed += [entry(False, True) for _ in range(n + 2)]
        trace = directed + trace
    return trace


def configurations(suite):
    if suite == "extension":
        return [("HierarchicalPipeline", {"w": w}) for w in [1, 3, 8]] + \
               [("NestedHierarchy", {"w": w}) for w in [2, 5]] + \
               [("HierarchicalComb", {"w": w, "invert": b}) for w, b in [(1, False), (3, True), (8, False)]] + \
               [("VectorReset", {"n": n, "w": w}) for n, w in [(1, 1), (3, 4), (5, 8)]] + \
               [("RegisterIdioms", {"w": w}) for w in [1, 3, 9]] + \
               [("BundlePipeline", {"w": w}) for w in [1, 8]] + \
               [("SwitchMachine", {"w": w}) for w in [2, 5]] + \
               [("MoreBits", {"w": w, "repeat": r}) for w, r in [(2, 1), (3, 4), (8, 3)]] + \
               [("SignedDivision", {"w": w}) for w in [2, 3, 8]] + \
               [("HardwareHelper", {"w": w}) for w in [1, 7]] + \
               [("PartialGenerate", {"n": n, "w": w}) for n, w in [(2, 1), (4, 5)]] + \
               [("LiteralVector", {})] + \
               [("RegisterFile", {"n": n, "w": w}) for n, w in [(1, 1), (2, 3), (3, 4), (8, 7)]] + \
               [("Fifo", {"depth": n, "w": w}) for n, w in [(2, 3), (3, 4), (8, 7)]] + \
               [("ParameterLookup", {"w": w}) for w in [2, 8]] + \
               [("TabulatedVector", {"n": n, "w": w}) for n, w in [(1, 2), (4, 3)]] + \
               [("AsyncMemory", {"n": n, "w": w}) for n, w in [(2, 3), (3, 4), (8, 7)]] + \
               [("MuxPriorities", {"w": w}) for w in [2, 5, 9]] + \
               [("Arithmetic", {"aWidth": 65, "bWidth": 7}), ("Arithmetic", {"aWidth": 128, "bWidth": 65}),
                ("SignedArithmetic", {"w": 65}), ("ExtendedBits", {"w": 128, "shift": 3})] + \
               [("SyncMemory", {"n": n, "w": w}) for n, w in [(2, 3), (3, 4), (8, 65)]] + \
               [("InferredRegisters", {"w": w}) for w in [2, 8, 65]] + \
               [("InferredVectorReset", {"n": n, "w": w}) for n, w in [(1, 3), (4, 8)]] + \
               [("AliasedInitializer", {"w": w}) for w in [3, 8]]
    if suite == "edge":
        return [("ElaborationWidth", {"n": n}) for n in [1, 2, 3, 4, 7]] + \
               [("ElaborationLiteral", {"n": n}) for n in [1, 3, 4]] + \
               [("ExtendedBits", {"w": w, "shift": s}) for w, s in [(0, 1), (1, 2), (3, 0), (3, 5)]] + \
               [("ReservedPorts", {}), ("BoolState", {}), ("ResetFromInput", {"w": 3}),
                ("MixedReset", {"w": 3}), ("NamedBundle", {"w": 3})]
    if suite == "control":
        return [("ConditionalPriority", {"w": w}) for w in [1, 3, 7]]
    if suite == "smoke":
        return [("Counter", {"n": 3}), ("Arithmetic", {"aWidth": 1, "bWidth": 2}),
                ("SignedArithmetic", {"w": 1}), ("VecLoop", {"n": 2, "w": 3}),
                ("BoolGate", {"invert": True})]
    cases = []
    cases += [("Counter", {"n": n}) for n in [1, 2, 3, 4, 7, 8, 9]]
    cases += [("DirectCounter", {"n": 3})]
    cases += [("DualRegister", {"n": n}) for n in [1, 3, 9]]
    cases += [("Priority", {"n": n}) for n in [1, 3, 8]]
    cases += [("ParamChoice", {"n": n}) for n in [1, 3, 4, 9]]
    cases += [("ParamExpression", {"n": n}) for n in [1, 3, 9]]
    cases += [("RenamedAccumulator", {"width": w, "increment": i}) for w, i in [(3, 3), (9, 7)]]
    cases += [("Arithmetic", {"aWidth": a, "bWidth": b}) for a, b in [(1, 2), (2, 1), (3, 3), (7, 3)]]
    cases += [("SignedArithmetic", {"w": w}) for w in [1, 3, 4]]
    cases += [("CombinationalDependencies", {"w": w}) for w in [3, 9]]
    cases += [("BoolGate", {"invert": value}) for value in [False, True]]
    cases += [("HelperWidth", {"n": n}) for n in [1, 3]]
    cases += [("EnabledRegister", {"w": w}) for w in [3, 9]]
    cases += [("VecLoop", {"n": n, "w": w}) for n, w in [(2, 3), (3, 1), (4, 7)]]
    cases += [("VecRegisters", {"n": n, "w": w}) for n, w in [(3, 2), (1, 8)]]
    cases += [("BitOperations", {"w": w}) for w in [1, 3, 8]]
    cases += [("VecIndex", {"n": 3, "w": 4, "k": k}) for k in [0, 2]]
    return cases


def run_case(ir_path, parameters, evidence):
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    trace = make_trace(ir, parameters)
    (evidence / "case.json").write_text(json.dumps({"module": ir["module"], "parameters": parameters,
        "seed": 20261013, "cycles": len(trace), "sampling": "before/after rising edge",
        "comparison_start": "post first reset/warm-up edge for stateful modules"}, indent=2), encoding="utf-8")
    initial = initial_state(ir, parameters)
    code, lean = simulate(ir_path, parameters, trace, initial, evidence / "lean", allow_undefined_initial_output=True)
    if code:
        return {"status": "FAIL", "phase": "lean-execution", "exit_code": code}
    code, rtl = run_rtl(ir_path, parameters, trace, evidence / "reference")
    if code:
        return {"status": "FAIL", "phase": "rtl", "exit_code": code}
    if len(lean) != len(rtl):
        return {"status": "FAIL", "phase": "trace-length"}
    compared = 0
    stateful = any(s["kind"] == "reg" for s in ir["signals"])
    for cycle, (actual, expected) in enumerate(zip(lean, rtl)):
        for sampling in ["pre", "post"]:
            if stateful and cycle == 0 and sampling == "pre":
                continue  # RTL power-on state is not a known reset state.
            a = flat_outputs(actual[sampling], ir["signals"])
            b = expected[sampling]
            if a != b:
                mismatch = {"cycle": cycle, "sampling": sampling, "lean": a, "rtl": b}
                (evidence / "mismatch.json").write_text(json.dumps(mismatch, indent=2), encoding="utf-8")
                return {"status": "FAIL", "phase": "comparison", **mismatch}
            compared += len(a)
    return {"status": "PASS", "cycles": len(trace), "compared_output_values": compared}


def fingerprint(ir_path, parameters):
    digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode())
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    generated_root = ir_path.parents[len(ir["module"].split(".")) - 1]
    files = {ir_path, ir_path.with_suffix("").with_suffix(".lean"),
             generated_root / "Chisel2Lean" / "Runtime.lean", Path(ir["source"]["file"]),
             ROOT / "reference" / "src" / "main" / "scala" / "GenerateRTL.scala"}
    files.update(Path(s["source"]["file"]) for s in ir["signals"])
    files.update(ROOT / "scripts" / name for name in ["differential.py", "simulate.py", "rtl.py", "common.py"])
    files.add(ROOT / "python/chisel2lean4/runtimes/parameterized.py")
    for path in sorted(files):
        digest.update(str(path.resolve()).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["smoke", "full", "edge", "control", "extension"], default="smoke")
    parser.add_argument("--generated", type=Path, default=ROOT / "generated")
    parser.add_argument("--evidence", type=Path, default=ROOT / "experiments" / "differential")
    parser.add_argument("--case", help="run one module's configurations")
    parser.add_argument("--resume", action="store_true", help="retain passed cases for the same unchanged sources/toolchain")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    results = []
    for index, (name, parameters) in enumerate(configurations(args.suite)):
        if args.case and args.case != name:
            continue
        label = name + "-" + "-".join(f"{k}{v}" for k, v in parameters.items())
        case = args.evidence / label
        case.mkdir(parents=True, exist_ok=True)
        saved = case / "summary.json"
        ir_path = args.generated.resolve() / "probes" / (name + ".ir.json")
        signature = fingerprint(ir_path, parameters)
        if args.resume and saved.is_file() and json.loads(saved.read_text())["status"] == "PASS" and json.loads(saved.read_text()).get("fingerprint") == signature:
            result = json.loads(saved.read_text())
        else:
            print(f"Running {label}", flush=True)
            start = time.monotonic()
            try:
                result = run_case(ir_path, parameters, case.resolve())
            except Exception as error:
                result = {"status": "FAIL", "phase": "harness", "error": str(error)}
            result.update(module=name, parameters=parameters, evidence=str(case), seconds=round(time.monotonic() - start, 2), fingerprint=signature)
            saved.write_text(json.dumps(result, indent=2), encoding="utf-8")
        results.append(result)
        print(f"{label}: {result['status']} ({result.get('phase', 'Lean/RTL agree')})", flush=True)
        if result["status"] != "PASS":
            print(json.dumps(result, indent=2), flush=True)
            break
    report = {"suite": args.suite, "cases": results,
              "passed": sum(r["status"] == "PASS" for r in results),
              "failed": sum(r["status"] != "PASS" for r in results),
              "formal_equivalence_proof": False}
    (args.evidence / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
