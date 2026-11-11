#!/usr/bin/env python3
"""
Lean translation checker for Verilog-to-Lean benchmark.

Scans Leans-Handcrafted/, Leans-Chat/, Leans-Gemmini/,
Leans-Chat_Repair/, and Leans-Gemmini_Repair/ for .lean files
and checks each for:
  Level 0: Per-file compilation success (via `lean <file>`)
  Level 1: Structural checks
    - No `sorry` usage
    - Has required declarations (Inputs/State, Outputs/Output, eval/step)
    - Namespace present
    - Has at least one theorem/lemma/example

Usage:
  python3 check.py                             # check all tiers, all non-repair sources
  python3 check.py --tier 2                    # only Tier 2
  python3 check.py --source chat               # only ChatGPT-generated files
  python3 check.py --source handcrafted        # only hand-crafted files
  python3 check.py --source gemmini            # only Gemmini-generated files
  python3 check.py --source chat_repair        # only Chat repair iterations
  python3 check.py --source gemmini_repair     # only Gemmini repair iterations
  python3 check.py --source chat_repair --iter 2   # only Chat repair iter 2
  python3 check.py --csv                       # output as CSV
  python3 check.py --logs                      # write per-module .log logs to logs/
  python3 check.py --no-compile                # structural checks only (faster)
"""

import subprocess
import sys
import re
import csv
import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

# Base sources (flat: <dir>/Tier N/*.lean)
SOURCE_DIRS = {
    "handcrafted": PROJECT_ROOT / "Leans-Handcrafted",
    "chat":        PROJECT_ROOT / "Leans-Chat",
    "gemmini":     PROJECT_ROOT / "Leans-Gemmini",
}

# Repair sources (nested: <dir>/Iter N/Tier N/*.lean)
REPAIR_SOURCE_DIRS = {
    "chat_repair":    PROJECT_ROOT / "Leans-Chat-Repair",
    "gemmini_repair": PROJECT_ROOT / "Leans-Gemmini-Repair",
}

ALL_SOURCES        = ["handcrafted", "chat", "gemmini"]
ALL_REPAIR_SOURCES = ["chat_repair", "gemmini_repair"]

LOG_DIR = PROJECT_ROOT / "logs"


# ---------------------------------------------------------------------------
# Compilation check (per-file)
# ---------------------------------------------------------------------------

def compile_lean_file(path: Path) -> tuple[bool, bool, str]:
    """Compile a single .lean file.

    Returns:
        (compiles, sorry_in_output, full_output)
        - compiles: True if exit code 0
        - sorry_in_output: True if the compiler printed a 'declaration uses sorry' warning
    """
    result = subprocess.run(
        ["lean", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    success = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    sorry_warning = bool(re.search(r"declaration uses [`']sorry[`']", output))
    return success, sorry_warning, output


# ---------------------------------------------------------------------------
# Structural checks (Level 1)
# ---------------------------------------------------------------------------

def check_structural(path: Path) -> dict:
    text = path.read_text()
    r = {}

    # Check for sorry on non-comment lines
    code_lines = [l for l in text.splitlines() if not l.lstrip().startswith("--")]
    code_text = "\n".join(code_lines)
    r["sorry_free"]          = not bool(re.search(r"\bsorry\b", code_text))
    r["has_namespace"]       = bool(re.search(r"^namespace\s+\S+", text, re.MULTILINE))
    r["has_eval"]            = bool(re.search(r"^def eval\b", text, re.MULTILINE))
    r["has_step"]            = bool(re.search(r"^def step\b", text, re.MULTILINE))
    r["has_inputs_or_state"] = bool(re.search(r"^structure\s+(Inputs|State)\b", text, re.MULTILINE))
    r["has_outputs"]         = bool(re.search(r"^structure\s+(Outputs|Output)\b", text, re.MULTILINE))
    # Handles plain `theorem`, `@[simp] theorem`, `@[simp] lemma`, `example`, etc.
    theorem_pattern = re.compile(r"^\s*(?:@\[.*?\]\s*)*(theorem|lemma|example)\b", re.MULTILINE)
    r["has_theorems"]        = bool(theorem_pattern.search(text))
    r["theorem_count"]       = len(theorem_pattern.findall(text))

    if r["has_eval"] and not r["has_step"]:
        r["module_type"] = "combinational"
    elif r["has_step"]:
        r["module_type"] = "sequential"
    else:
        r["module_type"] = "unknown"

    r["schema_ok"] = (
        r["has_namespace"]
        and r["has_inputs_or_state"]
        and r["has_outputs"]
        and (r["has_eval"] or r["has_step"])
        and r["sorry_free"]
    )
    return r


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(tiers: list[int], sources: list[str], iters: list[int]) -> list[dict]:
    """Return list of dicts with keys: tier, source, iter (or None), path."""
    entries = []

    for source in sources:
        # --- Base sources: <dir>/Tier N/*.lean ---
        if source in SOURCE_DIRS:
            base = SOURCE_DIRS[source]
            if not base.exists():
                continue
            for tier_dir in sorted(base.iterdir()):
                m = re.match(r"Tier\s*(\d+)", tier_dir.name, re.IGNORECASE)
                if not m:
                    continue
                tier = int(m.group(1))
                if tiers and tier not in tiers:
                    continue
                for f in sorted(tier_dir.glob("*.lean")):
                    entries.append({"tier": tier, "source": source, "iter": None, "path": f})

        # --- Repair sources: <dir>/Iter N/Tier N/*.lean ---
        elif source in REPAIR_SOURCE_DIRS:
            base = REPAIR_SOURCE_DIRS[source]
            if not base.exists():
                continue
            for iter_dir in sorted(base.iterdir()):
                m_iter = re.match(r"Iter\s*(\d+)", iter_dir.name, re.IGNORECASE)
                if not m_iter:
                    continue
                it = int(m_iter.group(1))
                if iters and it not in iters:
                    continue
                for tier_dir in sorted(iter_dir.iterdir()):
                    m_tier = re.match(r"Tier\s*(\d+)", tier_dir.name, re.IGNORECASE)
                    if not m_tier:
                        continue
                    tier = int(m_tier.group(1))
                    if tiers and tier not in tiers:
                        continue
                    for f in sorted(tier_dir.glob("*.lean")):
                        entries.append({"tier": tier, "source": source, "iter": it, "path": f})

    return entries


# ---------------------------------------------------------------------------
# Log generation
# ---------------------------------------------------------------------------

def write_log(entry: dict, struct: dict, compiles: bool, sorry_warned: bool, error_output: str) -> None:
    tier   = entry["tier"]
    source = entry["source"]
    it     = entry["iter"]
    module = entry["path"].stem

    # logs/Iter1/Tier4/divider_chat_repair.log  OR  logs/Iter0/Tier4/divider_chat.log
    iter_label = it if it is not None else 0
    log_path = LOG_DIR / f"Iter{iter_label}" / f"Tier{tier}" / f"{module}_{source}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    sorry_ok = struct["sorry_free"] and not sorry_warned
    status   = "PASS" if (compiles and struct["schema_ok"] and sorry_ok) else "FAIL"
    date     = datetime.date.today().isoformat()

    header = f"## {module} ({source})"
    if it is not None:
        header += f" — Iter {it}"

    lines = [
        header,
        f"",
        f"- **Date**: {date}",
        f"- **Tier**: {tier}",
        f"- **Source**: {source}",
    ]
    if it is not None:
        lines.append(f"- **Repair iter**: {it}")
    lines += [
        f"- **Module type**: {struct['module_type']}",
        f"",
        f"### Level 0: Compilation",
        f"- Compiles: {'yes' if compiles else 'NO'}",
        f"- Sorry axiom in compiler output: {'YES (warning)' if sorry_warned else 'no'}",
    ]
    if not compiles or sorry_warned:
        lines += [f"- Compiler output:", "```", error_output or "(no output)", "```"]

    lines += [
        f"",
        f"### Level 1: Structural checks",
        f"- Sorry-free (source text): {'yes' if struct['sorry_free'] else 'NO'}",
        f"- Has namespace: {'yes' if struct['has_namespace'] else 'NO'}",
        f"- Has Inputs/State: {'yes' if struct['has_inputs_or_state'] else 'NO'}",
        f"- Has Outputs/Output: {'yes' if struct['has_outputs'] else 'NO'}",
        f"- Has eval/step: {'yes' if (struct['has_eval'] or struct['has_step']) else 'NO'}",
        f"- Has theorems: {'yes' if struct['has_theorems'] else 'NO'} ({struct['theorem_count']})",
        f"- Schema OK: {'YES' if struct['schema_ok'] else 'NO'}",
        f"",
        f"### Overall status: {status}",
        f"",
        f"### Notes",
        f"<!-- Add manual observations here: error categories, repair rounds, etc. -->",
        f"",
    ]
    log_path.write_text("\n".join(lines))

    return log_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    all_source_choices = ALL_SOURCES + ALL_REPAIR_SOURCES

    parser = argparse.ArgumentParser(description="Check Lean translations")
    parser.add_argument("--tier",   type=int, choices=[1, 2, 3, 4],
                        help="Check only one tier")
    parser.add_argument("--source", choices=all_source_choices,
                        help="Check only one source (use chat_repair / gemmini_repair for repair dirs)")
    parser.add_argument("--iter",   type=int, choices=[0, 1, 2, 3],
                        help="Iteration to check: 0=original (chat/gemmini), 1-3=repair iterations")
    parser.add_argument("--repair", action="store_true",
                        help="Check all repair sources (chat_repair + gemmini_repair)")
    parser.add_argument("--csv",    action="store_true", help="Output results as CSV")
    parser.add_argument("--logs",   action="store_true",
                        help="Write per-module .log files to logs/")
    parser.add_argument("--no-compile", action="store_true",
                        help="Skip compilation (structural checks only)")
    args = parser.parse_args()

    tiers = [args.tier] if args.tier else []
    iters = [args.iter] if args.iter is not None else []

    # --iter 0 means "original files" — remap repair sources to their base equivalents
    REPAIR_TO_BASE = {"chat_repair": "chat", "gemmini_repair": "gemmini"}
    if args.iter == 0:
        if args.source:
            sources = [REPAIR_TO_BASE.get(args.source, args.source)]
        elif args.repair:
            sources = ALL_SOURCES  # all base sources
        else:
            sources = ALL_SOURCES
        iters = []  # base sources have no iter dimension
    elif args.source:
        sources = [args.source]
    elif args.repair:
        sources = ALL_REPAIR_SOURCES
    else:
        sources = ALL_SOURCES

    entries = collect_files(tiers, sources, iters)
    if not entries:
        print("No .lean files found.")
        sys.exit(0)

    rows = []
    for entry in entries:
        path   = entry["path"]
        struct = check_structural(path)

        compiles     = True
        sorry_warned = False
        error_output = ""
        if not args.no_compile:
            compiles, sorry_warned, error_output = compile_lean_file(path)
            compile_str = "yes" if compiles else "NO"
        else:
            compile_str = "skip"

        sorry_ok = struct["sorry_free"] and not sorry_warned

        # Short human-readable path used in error lists
        it = entry["iter"]
        if it is not None:
            rel_path = f"{entry['source']}/Iter {it}/Tier {entry['tier']}/{path.name}"
        else:
            rel_path = f"{entry['source']}/Tier {entry['tier']}/{path.name}"

        row = {
            "tier":         entry["tier"],
            "source":       entry["source"],
            "iter":         it if it is not None else "",
            "module":       path.stem,
            "rel_path":     rel_path,
            "compiles":     compile_str,
            "sorry_free":   "yes" if sorry_ok else "NO",
            "sorry_detail": ("text+compiler" if (struct["sorry_free"] and not sorry_warned)
                             else ("compiler_warn" if (struct["sorry_free"] and sorry_warned)
                             else "text")),
            "schema_ok":      "YES" if struct["schema_ok"] else "NO",
            "has_theorems":   "yes" if struct["has_theorems"] else "NO",
            "theorem_count":  struct["theorem_count"],
            "module_type":    struct["module_type"],
        }
        rows.append(row)

        if args.logs:
            log_path = write_log(entry, struct, compiles, sorry_warned, error_output)
            print(f"  Logged: {log_path.relative_to(PROJECT_ROOT)}")

        # Print per-file issues immediately
        if not compiles and not args.csv:
            print(f"\n[COMPILE ERROR] {rel_path}")
            print(error_output[:800])
            print()
        elif sorry_warned and not args.csv:
            print(f"\n[SORRY WARNING] {rel_path}")
            lines = [l for l in error_output.splitlines() if "sorry" in l.lower()]
            print("\n".join(lines[:5]))
            print()

    if args.csv:
        csv_fields = [k for k in rows[0].keys() if k != "rel_path"]
        writer = csv.DictWriter(sys.stdout, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return

    # -----------------------------------------------------------------------
    # Build the stats text (printed to stdout AND saved to logs/_stats.log)
    # -----------------------------------------------------------------------
    import io

    buf = io.StringIO()

    def out(s: str = "") -> None:
        print(s)
        buf.write(s + "\n")

    # Pretty table — add Iter column when repair sources are present
    has_iters = any(r["iter"] != "" for r in rows)
    if has_iters:
        col = f"{'T':<2} {'I':<2} {'Source':<15} {'Module':<35} {'Compiles':<10} {'Sorry-free':<12} {'Schema OK':<10} {'Thms':<6} {'Type':<14}"
    else:
        col = f"{'T':<2} {'Source':<15} {'Module':<35} {'Compiles':<10} {'Sorry-free':<12} {'Schema OK':<10} {'Thms':<6} {'Type':<14}"
    out(col)
    out("-" * len(col))
    for r in rows:
        if has_iters:
            out(
                f"{r['tier']:<2} {str(r['iter']):<2} {r['source']:<15} {r['module']:<35} "
                f"{r['compiles']:<10} {r['sorry_free']:<12} {r['schema_ok']:<10} {r['theorem_count']:<6} {r['module_type']:<14}"
            )
        else:
            out(
                f"{r['tier']:<2} {r['source']:<15} {r['module']:<35} "
                f"{r['compiles']:<10} {r['sorry_free']:<12} {r['schema_ok']:<10} {r['theorem_count']:<6} {r['module_type']:<14}"
            )

    out()
    total      = len(rows)
    compiled   = sum(1 for r in rows if r["compiles"] == "yes")
    sorry_free = sum(1 for r in rows if r["sorry_free"] == "yes")
    schema_ok  = sum(1 for r in rows if r["schema_ok"] == "YES")

    skipped = any(r["compiles"] == "skip" for r in rows)
    if not skipped:
        out(f"Compiles: {compiled}/{total}  |  Sorry-free: {sorry_free}/{total}  |  Schema-OK: {schema_ok}/{total}")
    else:
        out(f"Sorry-free: {sorry_free}/{total}  |  Schema-OK: {schema_ok}/{total}  (compilation skipped)")

    # Summary by source (and iter for repair sources)
    for src in all_source_choices:
        src_rows = [r for r in rows if r["source"] == src]
        if not src_rows:
            continue
        if src in ALL_REPAIR_SOURCES:
            iter_nums = sorted(set(r["iter"] for r in src_rows if r["iter"] != ""))
            for it in iter_nums:
                it_rows = [r for r in src_rows if r["iter"] == it]
                n = len(it_rows)
                c = sum(1 for r in it_rows if r["compiles"] == "yes")
                s = sum(1 for r in it_rows if r["schema_ok"] == "YES")
                if not skipped:
                    out(f"  [{src} iter={it}]  compiles={c}/{n}  schema-ok={s}/{n}")
                else:
                    out(f"  [{src} iter={it}]  schema-ok={s}/{n}")
        else:
            n = len(src_rows)
            c = sum(1 for r in src_rows if r["compiles"] == "yes")
            s = sum(1 for r in src_rows if r["schema_ok"] == "YES")
            if not skipped:
                out(f"  [{src}]  compiles={c}/{n}  schema-ok={s}/{n}")
            else:
                out(f"  [{src}]  schema-ok={s}/{n}")

    # Per-category error file lists
    def _print_failing(label: str, failing_rows: list[dict]) -> None:
        if not failing_rows:
            return
        out(f"\n{label} ({len(failing_rows)} file(s)):")
        for r in failing_rows:
            out(f"  {r['rel_path']}")

    if not skipped:
        _print_failing("Compilation errors", [r for r in rows if r["compiles"] == "NO"])

    _print_failing("Sorry violations",
                   [r for r in rows if r["sorry_free"] == "NO"])
    _print_failing("Schema failures (missing namespace/structures/eval/step)",
                   [r for r in rows if r["schema_ok"] == "NO"])
    _print_failing("No theorems",
                   [r for r in rows if r["has_theorems"] == "NO"])

    # -----------------------------------------------------------------------
    # Always write logs/<source>_<tier>_<iters>.log
    # -----------------------------------------------------------------------
    src_part  = "-".join(sources) if sources else "all"
    tier_part = "tier" + "-".join(str(t) for t in tiers) if tiers else "tierAll"
    # Base sources (chat/gemmini/handcrafted) are conceptually iter 0
    all_base = all(s in ALL_SOURCES for s in sources)
    iter_part = ("iter0" if args.iter == 0 or (not iters and all_base)
                 else "iter" + "-".join(str(i) for i in iters) if iters
                 else "iterAll")
    stats_name = f"{src_part}_{tier_part}_{iter_part}.log"

    stats_path = LOG_DIR / stats_name
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    header = f"# Stats — {date}  sources={','.join(sources)}  tiers={tiers or 'all'}  iters={iters or 'all'}\n\n"
    stats_path.write_text(header + buf.getvalue())
    print(f"\n[stats] Written to {stats_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
