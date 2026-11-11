# HardBench

**HardBench is a benchmark of kernel-checked hardware-verification theorems in Lean 4, derived from production hardware artifacts.** Every task is a Lean statement about a real RISC-V ISA spec, model-checking competition instance, or compiler pass; every "solved" verdict is a `lake env lean` kernel check with an audited axiom closure. We are not aware of a prior benchmark of this kind built from production artifacts rather than hand-written toy circuits.

The benchmark discriminates **between capability tiers**: on the 20-task calibration pilot, an open-weights 8B math prover solves 0/9 (attempted) and a frontier agentic prover solves 19/20. Beyond the pilot, a 25-task tier-2/3 ladder isolates the one axis that resists frontier-agentic proving in *every* scoring mode and protocol — **List/Nat induction over the stateful ISA spec** (T31, T33) — and a matched software arm plus an effort-gradient analysis show the resistance is **incidental spec mechanics, not the induction**. HardBench reports these gaps with 95% confidence intervals, a per-stratum failure taxonomy, and — via PROBE/FRONTIER — a **located** `bv_decide` scaling wall (~4–8k gates) that **reverses to laptop-tractable** once model-checker results are imported as LRAT certificates rather than re-solved.

> Status: research preview. This repo holds the 20-task pilot, the T21–T45 tier-2/3 ladder, the SwMatch software arm, the dual-mode scoring/integrity harness, a 556-item kernel-checked corpus, and the PROBE/FRONTIER scaling study. The full consolidated results are in [`results/FINAL_RESULTS.md`](results/FINAL_RESULTS.md).

## The three strata

Each stratum lives under `HardBench/` with a per-task `STATABILITY.md` mapping each statement to a real upstream identifier.

| Stratum | Dir | Source artifact (pin) | Asserted | Discharge | License |
|---|---|---|---|---|---|
| **Sail** (ISA spec) | `Sail/` | `opencompl/sail-riscv-lean` @ `c8b223ae` — Lean port of the official RISC-V Sail model (RV64I+M+Zb*, CSR/privileged) | pure-legalizer properties (idempotence, mask, range); AST discrimination/injectivity; monadic `execute_*` equations under fixed state | `simp [simp_sail]; bv_decide` / `injection` | **none found** — see caveat |
| **WordLevel** | `WordLevel/` | HWMCC'24 BTOR2 (Zenodo `zenodo.14156844`) + MetRex RTL (`scale-lab/MetRex`) via Yosys miter | bounded-safety to depth *k*; (in)equivalence of a module vs. a Yosys-`mutate` variant | `bv_decide` (+ witness for inequiv) | CC-BY-4.0 / BSD-3 |
| **Comb** | `Comb/` | `firtool`/`circt-opt` Comb pass pairs (CIRCT `firtool-1.153.1`) | before/after MLIR of a real pass are equivalent as `BitVec` functions | `bv_decide` | Apache-2.0 w/ LLVM-exc. |

Provenance is recorded twice — human-readable in each `STATABILITY.md`, machine-readable in `harness/tasks.json` (pins/checksums/licenses in `MANIFEST.toml`).

## Quickstart

```bash
# Build the library (Lean pinned to leanprover/lean4:v4.29.0; no mathlib)
cd hardbench && lake build

# Score an attempt: splice_proof.py grafts ONLY the prover's tactic body onto the
# frozen statement; check_axioms.sh kernel-checks + audits axioms.
python3 harness/splice_proof.py --statement-file results/statements/T01.lean \
  --theorem-fqn HardBench.Sail.legalize_xepc_idempotent \
  --prover-output-file <attempt>.txt --out-file /tmp/scored.lean
harness/check_axioms.sh /tmp/scored.lean HardBench.Sail.legalize_xepc_idempotent
# solved iff: exit 0, no `sorry`, axiom closure ⊆ whitelist
```

**Task registry format** (`harness/tasks.json`) — one row per task:

```json
{
  "id": "T01",
  "stratum": "sail",
  "lean_file": "HardBench/Sail/T01_LegalizeXepcIdempotent.lean",
  "theorem_fqn": "HardBench.Sail.legalize_xepc_idempotent",
  "provenance": "legalize_xepc idempotence (SysRegs.lean:1306) …",
  "imports_needed": ["LeanRV64D.SysRegs"]
}
```

`theorem_fqn` must be fully qualified (short names fail `#print axioms` resolution outside their namespace).

## Baselines

Full data and every source-file citation: [`results/FINAL_RESULTS.md`](results/FINAL_RESULTS.md). All four provers are externally kernel-verified through the splice + axiom-audit scorer. 95% CIs are Wilson score intervals (the recommended small-N/near-boundary estimator; at N = 2–20 they are wide **by construction**, and that width is part of the finding).

### Tier-1 calibration pilot (T01–T20, strict scoring)

| Prover | Configuration | sail | word | comb | Total | 95% CI (overall) |
|---|---|:--:|:--:|:--:|:--:|---|
| **Goedel-Prover-V2-8B** | open weights, Q4_K_M, CPU, 20-min cap, ~3 eff. samples/task | 0/7 | 0/2 | — | **0/9** | [0.0%, 29.9%] |
| **Claude no-tools** | single-shot, zero Lean execution | 2/7 | 7/7 | 6/6 | **15/20** | [53.1%, 88.8%] |
| **Claude agentic** | `lean-lsp` loop, references permission-locked | 6/7 | 7/7 | 6/6 | **19/20** | [76.4%, 99.1%] |
| **Aristotle (Harmonic)** | official CLI, v4.28 companion, spliced back | *⟨pending⟩* | 7/7 | 6/6 | **13/13** | [77.2%, 100%] (circuits) |

At the sail stratum the between-tier gap survives the CI framing: Goedel 0/7 [0, 35.4%] vs agentic 6/7 [48.7, 97.4%] do **not** overlap. Word/comb are saturated (every prover 100%) and carry no signal. Goedel is 0/9 **attempted** (16 samples launched/task, ~3–4 completed within the CPU wall; T10–T20 unrun on host OOM) — not 0/20; pending tasks are not scored as failures (`results/pilot/GOEDEL_CAVEAT.md`). Aristotle's 13/13 (720 s total, every proof via `bv_decide`) covers only the spec-independent circuit strata; **sail is ⟨pending⟩** — the v4.28 companion cannot load the v4.29 sail spec and the sail↔Aristotle bridge is unbuilt. Only agentic miss on the whole pilot: **T07** (monadic `execute_ITYPE`).

**Cross-system finding.** Three independent frontier systems (Claude agentic, Claude no-tools, Aristotle) all saturate the SAT-envelope circuit strata. Discrimination is not there — it is in spec-level monadic reasoning (no-tools sail drops to 2/7) and beyond-SAT scale.

### Tier-2/3 ladder (T21–T45) — dual-mode scoring

The ladder is scored **strict** (main tactic body only) and **helpers-allowed** (hardened H1 guard; adversarially attacked, 32/32 suite green). The discriminating result, per failure-family:

| Family (tasks) | Strict | Helpers | 95% CI (helpers) |
|---|:--:|:--:|---|
| Monadic rewrite-chain (T21–T24) | 1/4 | 4/4 | [51.0%, 100%] |
| Circuit-scale in SAT envelope (T25–T27) | 3/3 | 3/3 | [43.8%, 100%] |
| Witness search, hidden (T28–T30) | 3/3 | 3/3 | [43.8%, 100%] |
| Case-explosion (T32) | 0/1 | 1/1 | [20.7%, 100%] |
| **All induction (T31,33,34–45)** | **0/14** | **12/14** | **[60.1%, 96.0%]** |
| — induction × case-mixed (T42–T45) | 0/4 | 4/4 | [51.0%, 100%] |
| **All-protocol-resistant tip (T31, T33)** | **0/2** | **0/2** | **[0.0%, 65.8%]** |

**Strict scoring is the sharpest single discriminator on the induction stratum: 0/14 [0, 21.5%]** — these proofs legitimately need helper lemmas the strict policy discards. Helpers mode recovers 12/14 and leaves a **narrow 2-task resistant tip, T31 and T33** (ADDI-chain and CSR-write-seq List induction). These two resist **every** scoring mode **and** every protocol tried (strict, helpers-allowed, native_decide-warned, with remediation) — blocked not on the induction but on specific spec-mechanic reductions (`EStateM.bind` unfolding; the mcountinhibit CSR dispatcher). Case-explosion alone does **not** resist (T32 falls once the scorer recognizes inductive scaffolding — a scorer bug found and fixed, `results/round2/T32_INSPECTION.md`).

### Software arm + effort gradient — the incidental-complexity finding

**SwMatch** is a 6-task software comparison arm (a freshly-authored Lean 4 accumulator/IMP interpreter, no unlicensed upstream), structurally paired task-for-task to the hardware induction tasks, run under the **identical** protocol.

| Arm | Tasks | Helpers rate | Total wall | Remediation |
|---|---|---|---|---|
| Software (SwMatch) | 6 | 6/6 [61%, 100%] | **~17 min** | none, all first-attempt |
| Hardware (matched) | 11 paired | 9/11 [52%, 95%] | **~80 min + remediation** | 6 tasks needed a 2nd attempt |

The **binary** solve rates do not separate HW from SW at this N (overlapping CIs). The separation is in **effort**: at matched proof structure the hardware arm costs ~4–5× the wall time and needed a remediation round, while every software analogue solved first-try. The induction is not what's expensive — the **incidental spec mechanics are** (`EStateM.bind`, CSR dispatcher reduction, `Std.ExtDHashMap` `get?/insert` reasoning), present in the ISA spec and absent in the matched software interpreter. This is the honest answer to the NTP4VC positioning question (`docs/benchmark-landscape-recheck.md`): HardBench does not claim hardware induction is *harder in kind* than software induction; it shows the hardware substrate imposes a measurable incidental-mechanics tax at matched structure.

### Scaling: the bv_decide wall and its certificate-import reversal

On real HWMCC'24 AIGER instances at k=2 unrolling (`results/probe/FRONTIER.md`, `results/probe/PROBE.md`):

- **Path B (`bv_decide` re-solve): wall located between ~4,200 and ~8,000 unrolled AND-gates.** `deep_nested` (4,189 gates) proves in 12.3 s; `mod3_sep_reducer` (7,997 gates) times out at 1200 s. The corpus median (`elevator`, 241,012 gates) times out at 1797 s — this set PROBE's original **RED** verdict. RSS never bound (<900 MB); cost is CPU/elaboration-time, and wall is not smooth in gate count (structure, not size).
- **Path A (LRAT-Catcher certificate import): 🔴→🟢 REVERSED.** The exact instance that set the RED verdict checks via `lrat_reflect` in **6.08 s at 1.29 GB** — ~200× wall margin. Certificate import checks **every UNSAT point across the full probed range** (4,189 → 241,012 gates); **no upper bound located**. Peak RSS rises +8% for a 58× gate increase; certificates stay <5 KB. **Kernel-checked import of a model-checker refutation at competition-median scale is laptop-tractable.**

This does **not** overturn the Path-B finding — from-scratch `bv_decide` re-solving is still RED, independently reconfirmed. The reversal is Path-specific, and Path A's trust base includes `native_decide` (a wider TCB than pure `bv_decide`). Untested: k ≥ 3, instances beyond the corpus median.

**Provenance disclosure.** Tier-1 and induction-ladder reference statements were reference-proved by Claude-family agents, and the Claude baselines are Claude-family. Mitigations: references are **permission-locked** during scored runs; scoring is **external and kernel-checked** (splice + axiom audit), independent of the prover; Aristotle is a **non-Claude** frontier baseline on the circuit strata (a sail-stratum non-Claude run remains pending on the toolchain bridge). Weigh the Claude numbers with this shared-lineage caveat.

## Integrity design

An automated checker invites a prover that games it rather than proving the theorem. A pre-run adversarial review (`harness/CRITIC_REPORT.md`) found three critical holes; all are closed **structurally**, not by exploit-patching.

- **Splice-don't-trust** (fix F1). The file reaching the kernel checker is never the prover's own text — it is always the *frozen* `results/statements/<ID>.lean` (the un-editable signature) with only its final `sorry` replaced by a body extracted from the prover's output. Kills C1 (statement substitution — cannot weaken the theorem), C2 (a prover-declared `axiom` is discarded preamble), and C3 (decoy `#print axioms`) by construction.
- **Axiom whitelist.** A solve's `#print axioms` closure must be ⊆ `{propext, Classical.choice, Quot.sound}` ∪ the theorem's own `._native.bv_decide.*` axioms ∪ the 75 axioms declared by the vendored `LeanRV64D`/`Sail` packages. `sorryAx` rejected unconditionally; `native_decide` fails (non-whitelisted `Lean.ofReduceBool`).
- **Permission-locked references.** Reference proofs are locked out of scope during runs; every solve is re-checked independently with the audit above.

## Honest limitations

- **Tier-1 saturates for frontier-agentic provers.** The pilot is a *calibration* set; at 19/20 the mechanical gate flagged it "trivial tier" (`gate_verdict.json`: `NO_GO_KILL`). Within-frontier discrimination lives in the tier-2/3 ladder, where the load-bearing result is strict-mode induction (0/14) and the 2-task all-protocol resistant tip (T31, T33).
- **The resistant tip is narrow and noisy.** T31/T33 resist all protocols, but their near-identical siblings (T38 reuses T31's chain; T41 is T33's companion CSR) are helpers-solved. That a task and its sibling land on opposite sides is itself small-N verdict variance — read the "2-task tip" as real but not a robust, reproducible boundary (`results/FINAL_RESULTS.md` §Inconsistencies).
- **Scale: two paths, opposite verdicts (PROBE/FRONTIER).** `bv_decide` re-solving hits a wall between ~4–8k unrolled AND-gates at k=2 (RED, `results/probe/FRONTIER.md`). **But** LRAT certificate import (LRAT-Catcher) checks the same instances — including the 241k-gate median that set the original RED verdict — in single-digit seconds at ~1.3 GB (🔴→🟢, `results/probe/FRONTIER.md` §reversal). Certifier claims are **no longer scoped to the sub-median subset for the certificate-import path**; the `bv_decide`-restatement path remains sub-frontier only. Untested beyond the corpus median and k ≥ 3; Path A's TCB includes `native_decide`.
- **Corpus is 556 items, below the 1000 spec target.** Families are exhausted at current template richness; expansion levers are documented but unspent.
- **Verification-cost motivation is folklore.** We do **not** anchor HardBench on the "~70% of hardware effort is verification" figure; the primary source (Foster, DAC'15) measures ~57%, and Foster himself calls 70% unsubstantiated. Motivation stays qualitative.

## Licenses

HardBench's harness and task **statements** are licensed **Apache-2.0** (see `LICENSE` at the repo root; copyright "HardBench contributors"). Task **inputs** remain governed **per-artifact**, independent of the harness license (`MANIFEST.toml`): **HWMCC'24** — CC-BY-4.0; **MetRex** — BSD-3-Clause (we derive from MetRex, never the unlicensed VeriThoughts repo); **CIRCT/firtool** — Apache-2.0 WITH LLVM-exceptions; **Goedel-Prover-V2-8B GGUF** — base model card (not re-verified). Third-party license text/attribution is bundled under `corpus/metrex_mutation/` and `corpus/btor2_ladder/`.

> Note: the Apache-2.0 choice above is **staged pending the repo owner's confirmation at release sign-off** — see `RELEASE_REVIEW.md`'s remediation log for B3.

⚠️ **`sail-riscv-lean` has no LICENSE file upstream** at the pinned commit. It is consumed as a commit-pinned `lake` dependency, not re-vendored. Downstream users must resolve this gap before redistributing Sail-derived tasks — a known open issue.

## Pin & refresh policy

The upstream RISC-V Sail model (`opencompl/sail-riscv-lean`) **regenerates daily** from the Sail source — statements written against it would silently drift. HardBench freezes this out-of-scope with an explicit policy (the benchmark-lifecycle literature flags exactly this failure mode; `docs/benchmark-landscape-recheck.md` §4):

- **Sail tasks pin `sail-riscv-lean` commit `c8b223ae`.** No Sail-derived statement is scored against any other revision. The LRAT-Catcher scaling artifact is separately pinned to commit `4ec2168b810636e789da3349ab3e670af338187c` (its own `v4.30.0` toolchain, built in isolation). All pins, checksums, and per-artifact licenses live in `MANIFEST.toml`.
- **Regeneration cadence is versioned, not silent.** A refresh is a **new pinned version** of the benchmark, never an in-place edit of the current one. On refresh: re-pin the upstream commit, **re-validate every statement** still elaborates against the new revision (the day-1 statability gate: `theorem … := by sorry` must elaborate to count), re-run the axiom audit, and freeze the prior version rather than overwrite it.
- **Non-Sail strata are pinned to inert artifacts** (HWMCC'24 Zenodo `zenodo.14156844`, MetRex commit, CIRCT `firtool-1.153.1`) that do not auto-regenerate, so they carry the same pin discipline without the daily-drift risk.

## Roadmap

Delivered since the pilot (in this repo): the **T21–T45 tier-2/3 ladder**, the **SwMatch software arm**, **dual-mode scoring** (strict + hardened helpers-allowed), a **556-item kernel-checked corpus** (`corpus/`), the **PROBE/FRONTIER scaling study** with the certificate-import reversal, and Wilson-CI framing throughout — consolidated in [`results/FINAL_RESULTS.md`](results/FINAL_RESULTS.md). Still planned: **corpus expansion toward ≥1k items** along the documented levers (more mutation classes / pass configs / instances); a **non-Claude sail-stratum baseline** once the Aristotle↔sail toolchain bridge lands; and public **dataset + paper** release (`docs/paper-draft-v0.md`) pending user sign-off.

*Layout:* `HardBench/{Sail,WordLevel,Comb}/` (task `.lean` + `STATABILITY.md`), `harness/` (scorer + drivers + `tasks.json`), `results/{statements,pilot,probe}/`, `corpus/` (generators, in dev), `MANIFEST.toml` (pins, checksums, licenses).
