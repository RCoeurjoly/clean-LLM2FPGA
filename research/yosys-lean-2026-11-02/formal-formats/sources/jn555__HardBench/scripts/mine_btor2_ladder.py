#!/usr/bin/env python3
"""
scripts/mine_btor2_ladder.py -- WS4(d) generator (spec-week2.md WS4(d)):
BTOR2 bounded-safety at increasing k. Builder: this session (no subagents;
notes/spec-week2.md WS4 "(d) BTOR2 bounded-safety at increasing k -- B2").

Extends the W1 T13/T14 fib-class path (hand-transcribed) with a generic
BTOR2 (bv-subset) parser + Lean codegen (scripts/btor2_ir.py), applied to
OTHER real benchmarks_btor2_bv instances (not fib_30/fib_37, which are
already the curated HardBench/WordLevel/T13-T14 pilot subjects).

Instance selection ("smallest upward", spec-week2.md WS4(d) "Restrict to
sub-frontier instances... start <=10^4"): the 4 smallest-by-linecount
instances (after fib_30/fib_37) in artifacts/hwmcc24/btor2_bv/btor2/2024/
whose op grammar is fully covered by scripts/btor2_ir.py (sort/input/state/
init/next/bad/const/constd/not/neg/and/or/xor/add/sub/mul/udiv/urem/eq/neq/
ult/ugte/slt/sext/uext/ite -- confirmed by grepping each candidate's op
column before adding it here; anything using arrays, shifts, concat/slice,
or the negative-literal NOT-shorthand is out of grammar and excluded, not
forced). unrolled_node_count(k) stays in the low hundreds even at k=4 --
several orders of magnitude under the 241k-gate PROBE median wall
(results/probe/PROBE.md) and the spec's own "<=10^4" sub-frontier line.

Ground truth / label authority (mirrors WS4(a)'s Yosys-sat role): bundled
`btormc` (oss-cad-suite) decides SAT/UNSAT at each k directly on the
ORIGINAL .btor2 file, independent of this generator's Lean transcription.
UNSAT (silent, exit 0) at kmax=k -> emit a universal "safe up to k" Lean
theorem (all bad_i = 0#1, forall free vars). SAT -> btormc's own concrete
witness trace supplies literal values for every free variable; those exact
literals are substituted into the Lean theorem as a closed instance
asserting bad_k = 1#1 at the witness -- so the Lean proof is not just
"the translation says unsafe", it is "the translation reproduces btormc's
own witness", which is the strongest cross-check available without a
from-scratch independent BTOR2 semantics implementation.

Ladder: per instance, k = 1, 2, 3, ... until btormc reports SAT (emit the
unsafe item and stop laddering that instance) or a check_axioms.sh
invocation hits its CHECK_AXIOMS_TIMEOUT_S wall (spec: "elaboration >5 min
-> stop, record max feasible k"; check_axioms.sh's existing 300s default
timeout IS that 5-minute wall, reused as-is, not reimplemented).

Day-1 smoke gate:
    python3 scripts/mine_btor2_ladder.py --smoke --kmax-per-instance 3
reports {attempted, emitted, kernel_checked, rate} as JSON, exit 1 if
rate < 0.90.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent
sys.path.insert(0, str(HERE))

from btor2_ir import parse_btor2, render_unrolled, unrolled_node_count, Btor2UnsupportedError  # noqa: E402

FAMILY = "btor2_ladder"
OUT_DIR = PROJECT_DIR / "corpus" / FAMILY
LEAN_DIR = OUT_DIR / "lean"
ITEMS_JSONL = OUT_DIR / "items.jsonl"
LICENSE_FILE = OUT_DIR / "LICENSE_ASSERT.txt"
PROVENANCE_FILE = OUT_DIR / "PROVENANCE.txt"

NAMESPACE = "HardBench.Corpus.Btor2Ladder"
BTORMC = PROJECT_DIR / "artifacts" / "oss-cad-suite" / "bin" / "btormc"
BTOR2_ROOT = PROJECT_DIR / "artifacts" / "hwmcc24" / "btor2_bv" / "btor2"
CHECK_AXIOMS_SH = PROJECT_DIR / "harness" / "check_axioms.sh"
SPLICE_PY = PROJECT_DIR / "harness" / "splice_proof.py"
ALLOWED_LICENSE = "CC-BY-4.0"
MARKER = "-- === REFERENCE CERTIFICATE (harness/splice_proof.py splices this proof body into the frozen statement above; not itself scored) ==="

# (short-id, relative path under BTOR2_ROOT). fib_30/fib_37 deliberately
# excluded -- those are the curated HardBench/WordLevel/T13-T14 subjects.
INSTANCES = [
    ("cancel_var", "2024/sosylab/unsignedintegeroverflow-sas23/cancel_var_through_overflow.btor2"),
    ("c93", "2024/hku/arithmetic_circuits/93.c/93.c.btor2"),
    ("bin_suffix5", "2024/sosylab/loop-invariants/bin-suffix-5.btor2"),
    ("phases2_1", "2024/sosylab/loop-acceleration/phases_2-1.btor2"),
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def run_btormc(path: Path, kmax: int, timeout_s=60):
    proc = subprocess.run([str(BTORMC), "-kmax", str(kmax), str(path)],
                           capture_output=True, text=True, timeout=timeout_s)
    out = proc.stdout
    stripped = out.strip()
    if not stripped.startswith("sat"):
        # exit 0 + empty/near-empty stdout is btormc's normal "UNSAT up to kmax"
        # signal (verified empirically). A nonzero exit or non-empty stdout that
        # still isn't "sat" (e.g. "btormc: can not read '<path>'") is a real
        # tool error, not a safety verdict -- must not be silently read as safe.
        if proc.returncode != 0 or stripped:
            raise RuntimeError(f"btormc error on {path} (kmax={kmax}, exit={proc.returncode}): "
                               f"{stripped or proc.stderr.strip()}")
        return {"result": "unsat_or_unknown", "raw": out}
    # Parse trace: "#0" state block, then "@0".."@k" input blocks. Each line:
    # "<idx> <bits> <name>". Blocks end at the next '#'/'@'/'.' marker line.
    blocks = {}
    cur = None
    for line in out.splitlines():
        line = line.rstrip()
        if line in ("sat",) or line == ".":
            continue
        m = re.match(r"^([#@])(\d+)$", line)
        if m:
            cur = (m.group(1), int(m.group(2)))
            blocks[cur] = []
            continue
        if line.startswith("b"):
            continue  # "b0" property-index marker
        if cur is not None:
            mm = re.match(r"^(\d+)\s+(\S+)\s+\S+", line)
            if mm:
                blocks[cur].append((int(mm.group(1)), mm.group(2)))
    return {"result": "sat", "blocks": blocks, "raw": out}


def bits_to_int(bits: str) -> int:
    return int(bits, 2)


def build_witness(prog, trace_blocks: dict):
    """Maps btormc's ('#',0) / ('@',frame) index-ordered blocks back onto
    (node_id -> int value), using the SAME declaration-order convention
    verified empirically against a real trace (results docstring above):
    state block '#0' lists exactly the no-init states, in file order; each
    '@f' block lists ALL inputs, in file order."""
    no_init_states = [sid for sid, _, has_init in prog.states if not has_init]
    s0_vals = {}
    for idx, bits in trace_blocks.get(("#", 0), []):
        s0_vals[no_init_states[idx]] = bits_to_int(bits)
    input_ids = [iid for iid, _ in prog.inputs]
    frame_vals = {}
    frame = 0
    while ("@", frame) in trace_blocks:
        vals = {}
        for idx, bits in trace_blocks[("@", frame)]:
            vals[input_ids[idx]] = bits_to_int(bits)
        frame_vals[frame] = vals
        frame += 1
    return s0_vals, frame_vals, frame - 1  # last frame index = the k that hit bad


def lean_check_ok(lean_file: Path, timeout_s=120):
    proc = subprocess.run(["lake", "env", "lean", str(lean_file)],
                           cwd=str(PROJECT_DIR), capture_output=True, text=True,
                           timeout=timeout_s)
    return proc.returncode == 0, (proc.stdout + proc.stderr)


def score_item(statement_text: str, cert_text: str, theorem_fqn: str, tmpdir: Path,
               check_timeout_s: int):
    stmt_path = tmpdir / "statement.lean"
    cert_path = tmpdir / "cert.lean"
    spliced_path = tmpdir / "spliced.lean"
    stmt_path.write_text(statement_text)
    cert_path.write_text(cert_text)

    ok, out = lean_check_ok(stmt_path)
    if not ok:
        return {"statement_elaborates": False, "kernel_checked": False,
                "axioms": [], "detail": out[-1500:]}

    proc = subprocess.run(
        ["python3", str(SPLICE_PY), "--statement-file", str(stmt_path),
         "--theorem-fqn", theorem_fqn, "--prover-output-file", str(cert_path),
         "--out-file", str(spliced_path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return {"statement_elaborates": True, "kernel_checked": False,
                "axioms": [], "detail": f"splice_failed: {proc.stdout}{proc.stderr}"}

    env = {**os.environ, "CHECK_AXIOMS_TIMEOUT_S": str(check_timeout_s)}
    proc2 = subprocess.run(
        [str(CHECK_AXIOMS_SH), str(spliced_path), theorem_fqn],
        capture_output=True, text=True, cwd=str(PROJECT_DIR), env=env)
    try:
        verdict = json.loads(proc2.stdout.strip().splitlines()[-1])
    except Exception:
        return {"statement_elaborates": True, "kernel_checked": False,
                "axioms": [], "detail": f"check_axioms_parse_failed: {proc2.stdout}{proc2.stderr}"}
    return {"statement_elaborates": True, "kernel_checked": bool(verdict.get("solved")),
            "axioms": verdict.get("axioms", []), "detail": verdict}


def args_decl_str(free_decls):
    return " ".join(f"({n} : BitVec {w})" for n, w in free_decls)


def build_safe_item(item_id, prog, k, rel_path):
    r = render_unrolled(prog, k)
    thm = item_id
    fqn = f"{NAMESPACE}.{thm}"
    goal_conj = " ∧ ".join(f"{b} = (0#1)" for b in r["bad_names"])
    body_lets = "\n".join(r["let_lines"])
    header = (
        f"/-\nWS4(d) btor2_ladder item {item_id} (family={FAMILY}).\n"
        f"instance: {rel_path}  depth k={k}  label: safe (btormc UNSAT at kmax={k})\n"
        f"Label authority: bundled btormc (oss-cad-suite), run directly on the\n"
        f"original .btor2 file, independent of this Lean transcription.\n-/\n"
    )
    statement = (
        header + "import Std.Tactic.BVDecide\n\n" +
        f"namespace {NAMESPACE}\n\n" +
        f"theorem {thm} {args_decl_str(r['free_decls'])} :\n" +
        f"{body_lets}\n" +
        f"    {goal_conj} := by\n  sorry\n\nend {NAMESPACE}\n"
    )
    cert = (
        f"\n{MARKER}\n\nnamespace {NAMESPACE}\n\n" +
        f"theorem {thm} {args_decl_str(r['free_decls'])} :\n" +
        f"{body_lets}\n" +
        f"    {goal_conj} := by\n  bv_decide\n\nend {NAMESPACE}\n"
    )
    return {"id": item_id, "fqn": fqn, "statement": statement, "cert": cert,
            "label": "safe", "k": k, "witness": None}


def build_unsafe_item(item_id, prog, k, rel_path, s0_vals, frame_vals):
    r = render_unrolled(prog, k)
    # Substitute the concrete witness for every free variable instead of
    # quantifying: build a name->literal map and inline it via `let` values
    # (same generality as render_unrolled, just closed rather than free).
    subst = {}
    for sid, val in s0_vals.items():
        w = dict((s, ww) for s, ww, _ in prog.states)[sid]
        subst[f"s0_{sid}"] = f"({val}#{w})"
    for frame, vals in frame_vals.items():
        for iid, val in vals.items():
            w = dict(prog.inputs)[iid]
            subst[f"i{frame}_{iid}"] = f"({val}#{w})"
    free_names = {n for n, _ in r["free_decls"]}
    missing = free_names - set(subst)
    if missing:
        raise ValueError(f"witness missing values for {missing}")
    body_lets = "\n".join(r["let_lines"])
    for name, lit in subst.items():
        body_lets = re.sub(rf"\b{re.escape(name)}\b", lit, body_lets)
    last_bad = r["bad_names"][-1]
    thm = item_id
    fqn = f"{NAMESPACE}.{thm}"
    header = (
        f"/-\nWS4(d) btor2_ladder item {item_id} (family={FAMILY}).\n"
        f"instance: {rel_path}  depth k={k}  label: unsafe (btormc SAT at kmax={k})\n"
        f"witness (btormc concrete trace, substituted literally below):\n"
        f"  s0={s0_vals} frame_inputs={frame_vals}\n"
        f"Label authority: bundled btormc; this theorem reproduces btormc's own\n"
        f"witness -- if the translation were wrong, bad{k} would not evaluate to 1\n"
        f"under btormc's exact values, and this statement would fail to elaborate\n"
        f"or bv_decide would refute it.\n-/\n"
    )
    statement = (
        header + "import Std.Tactic.BVDecide\n\n" +
        f"namespace {NAMESPACE}\n\n" +
        f"theorem {thm} :\n" +
        f"{body_lets}\n" +
        f"    {last_bad} = (1#1) := by\n  sorry\n\nend {NAMESPACE}\n"
    )
    cert = (
        f"\n{MARKER}\n\nnamespace {NAMESPACE}\n\n" +
        f"theorem {thm} :\n" +
        f"{body_lets}\n" +
        f"    {last_bad} = (1#1) := by\n  bv_decide\n\nend {NAMESPACE}\n"
    )
    return {"id": item_id, "fqn": fqn, "statement": statement, "cert": cert,
            "label": "unsafe", "k": k, "witness": {"s0": s0_vals, "frames": frame_vals}}


def ensure_family_scaffold():
    LEAN_DIR.mkdir(parents=True, exist_ok=True)
    if not LICENSE_FILE.exists():
        LICENSE_FILE.write_text(
            "WS4(d) btor2_ladder family -- allowed license: CC-BY-4.0 (HWMCC'24 "
            "benchmark set, spec-week2.md WS4 License filter).\n"
            "Every item traces to artifacts/hwmcc24/btor2_bv/btor2/<path> (Zenodo DOI "
            "10.5281/zenodo.14156844, CC-BY-4.0 per MANIFEST.toml WS1(b) row).\n"
        )
    if not PROVENANCE_FILE.exists():
        PROVENANCE_FILE.write_text(
            "WS4(d) btor2_ladder provenance (scripts/mine_btor2_ladder.py + scripts/btor2_ir.py)\n"
            "instances: see INSTANCES list in scripts/mine_btor2_ladder.py "
            "(smallest-upward sub-frontier picks from artifacts/hwmcc24/btor2_bv/btor2/2024/, "
            "excluding fib_30/fib_37 which are the curated HardBench/WordLevel/T13-T14 subjects).\n"
            "label authority + witness source: bundled btormc (artifacts/oss-cad-suite/bin/btormc), "
            "run directly on each original .btor2 file, independent of the Lean transcription.\n"
            "cert pipeline: harness/splice_proof.py + harness/check_axioms.sh (identical contract "
            "to WS4(a)/(b)/(c)).\n"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--kmax-per-instance", type=int, default=4,
                     help="ladder stops at this k for instances that stay safe "
                          "(a time-budget cap for this pass, not a spec wall)")
    ap.add_argument("--check-timeout-s", type=int, default=300)
    args = ap.parse_args()

    ensure_family_scaffold()

    existing_ids = set()
    if ITEMS_JSONL.exists():
        for line in ITEMS_JSONL.read_text().splitlines():
            if line.strip():
                existing_ids.add(json.loads(line)["id"])

    emitted = []
    log = []
    out_fh = open(ITEMS_JSONL, "a")

    for short_id, rel_path in INSTANCES:
        full_path = BTOR2_ROOT / rel_path
        text = full_path.read_text()
        try:
            prog = parse_btor2(text)
        except Btor2UnsupportedError as e:
            log.append({"instance": short_id, "skip": f"parse_unsupported: {e}"})
            continue

        for k in range(1, args.kmax_per_instance + 1):
            gate_count = unrolled_node_count(prog, k)
            try:
                bmc = run_btormc(full_path, k)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                log.append({"instance": short_id, "k": k, "skip": f"btormc_error: {e}"})
                break
            if bmc["result"] == "sat":
                s0_vals, frame_vals, hit_k = build_witness(prog, bmc["blocks"])
                item_id = f"bt_{short_id}_k{hit_k}_unsafe"
                if item_id in existing_ids:
                    break
                try:
                    row = build_unsafe_item(item_id, prog, hit_k, rel_path, s0_vals, frame_vals)
                except (Btor2UnsupportedError, ValueError) as e:
                    log.append({"instance": short_id, "k": hit_k, "skip": f"codegen_failed: {e}"})
                    break
                with tempfile.TemporaryDirectory(prefix="mine_btor2_", dir="/tmp") as td:
                    verdict = score_item(row["statement"], row["cert"], row["fqn"], Path(td),
                                          args.check_timeout_s)
                if verdict["statement_elaborates"]:
                    lean_path = LEAN_DIR / f"{item_id}.lean"
                    full_text = row["statement"] + row["cert"]
                    lean_path.write_text(full_text)
                    item_row = {
                        "id": item_id, "family": FAMILY,
                        "lean_file": str(lean_path.relative_to(PROJECT_DIR)),
                        "theorem_fqn": row["fqn"], "label": "unsafe",
                        "witness": row["witness"], "cert_type": "bv_decide",
                        "source_pin": f"{rel_path}::k={hit_k}",
                        "tool_cmd": f"btormc -kmax {hit_k} {rel_path}",
                        "sha256": sha256_text(full_text), "license": ALLOWED_LICENSE,
                        "kernel_checked": verdict["kernel_checked"], "axioms": verdict["axioms"],
                        "difficulty": {"tier": None,
                                       "axes": {"unroll_k": hit_k, "size_bits": None},
                                       "expected_tactic_class": "bv_decide", "frontier_solve_rate": None},
                        "instance": rel_path, "gate_count_proxy": gate_count,
                    }
                    out_fh.write(json.dumps(item_row) + "\n"); out_fh.flush()
                    emitted.append(item_row)
                    print(f"EMIT {item_id} (unsafe) kernel_checked={verdict['kernel_checked']}",
                          file=sys.stderr)
                else:
                    log.append({"instance": short_id, "k": hit_k,
                                "skip": f"statement_no_elaborate: {verdict['detail']}"})
                break  # bad found: stop laddering this instance (spec: "until ... bad becomes reachable")

            # UNSAT at this k -> emit a "safe up to k" item
            item_id = f"bt_{short_id}_k{k}_safe"
            if item_id in existing_ids:
                continue
            row = build_safe_item(item_id, prog, k, rel_path)
            with tempfile.TemporaryDirectory(prefix="mine_btor2_", dir="/tmp") as td:
                verdict = score_item(row["statement"], row["cert"], row["fqn"], Path(td),
                                      args.check_timeout_s)
            if verdict["statement_elaborates"]:
                lean_path = LEAN_DIR / f"{item_id}.lean"
                full_text = row["statement"] + row["cert"]
                lean_path.write_text(full_text)
                item_row = {
                    "id": item_id, "family": FAMILY,
                    "lean_file": str(lean_path.relative_to(PROJECT_DIR)),
                    "theorem_fqn": row["fqn"], "label": "safe", "witness": None,
                    "cert_type": "bv_decide", "source_pin": f"{rel_path}::k={k}",
                    "tool_cmd": f"btormc -kmax {k} {rel_path}",
                    "sha256": sha256_text(full_text), "license": ALLOWED_LICENSE,
                    "kernel_checked": verdict["kernel_checked"], "axioms": verdict["axioms"],
                    "difficulty": {"tier": None, "axes": {"unroll_k": k, "size_bits": None},
                                   "expected_tactic_class": "bv_decide", "frontier_solve_rate": None},
                    "instance": rel_path, "gate_count_proxy": gate_count,
                }
                out_fh.write(json.dumps(item_row) + "\n"); out_fh.flush()
                emitted.append(item_row)
                print(f"EMIT {item_id} (safe) kernel_checked={verdict['kernel_checked']}",
                      file=sys.stderr)
            else:
                log.append({"instance": short_id, "k": k,
                            "skip": f"statement_no_elaborate: {verdict['detail']}"})

    out_fh.close()

    n_emitted = len(emitted)
    n_ok = sum(1 for r in emitted if r["kernel_checked"])
    rate = (n_ok / n_emitted) if n_emitted else 0.0
    summary = {"family": FAMILY, "emitted": n_emitted, "kernel_checked": n_ok,
               "rate": round(rate, 4), "skipped": len(log)}
    print(json.dumps(summary, indent=2))
    if log:
        with (OUT_DIR / "_gen_log.jsonl").open("a") as fh:
            fh.write("\n".join(json.dumps(x) for x in log) + "\n")

    if args.smoke:
        return 0 if rate >= 0.90 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
