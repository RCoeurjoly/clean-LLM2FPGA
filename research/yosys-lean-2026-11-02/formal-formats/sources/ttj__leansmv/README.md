# VerifDemo — Lean 4 Verification Demo

A Lean 4 verification demo covering:

1. **Discrete math** — set theory proofs (`VerifDemo/DiscreteMath.lean`)
2. **Transition systems** — reachable states, inductive invariants, k-induction,
   ranking functions for liveness (`VerifDemo/TransitionSystem.lean`)
3. **NuXMV translation** — parse `.smv` models and verify invariants in Lean
   (`scripts/smv2lean.py`, `VerifDemo/NuXMV/`)
4. **Parametric model checking** — N-process mutual exclusion for all N
   (`VerifDemo/Parametric/NMutex.lean`)
5. **Cellular flows** — full formalization of "Safe and Stabilizing Distributed
   Multi-Path Cellular Flows" (Johnson & Mitra, TCS 2015). ~6,000 lines,
   200+ theorems, 1D + 2D, both main theorems proved, continuous ℝ² model
   via Mathlib (`VerifDemo/CellularFlows/`)
6. **Program verification** — IMP language, big-step semantics, Hoare logic,
   verified insertion sort (`VerifDemo/ProgramVerif/`)

## Build

First-time setup (pulls Mathlib cache for the Euclidean continuous model):

```bash
lake update        # pulls Mathlib v4.29.0 (~5-10 min with cache)
lake exe cache get # pulls precompiled Mathlib .olean files
```

Then:

```bash
lake build
```

Full build: 2,425 jobs, zero `sorry`. Most of VerifDemo is Mathlib-free Lean 4
core; only `VerifDemo/CellularFlows/EuclideanModel.lean` uses Mathlib (for
real Euclidean distance in ℝ²).

Build a single module (faster iteration):

```bash
lake build VerifDemo.NuXMV.CounterProofs
lake build VerifDemo.CellularFlows.MultiColorProofs
```

## SMV → Lean translator

Generate a Lean file from an SMV model:

```bash
pip install -r scripts/requirements.txt       # first time only
python scripts/smv2lean.py models/counter.smv VerifDemo/NuXMV/Counter.lean
```

Supports:
- Enums (`{off, on}` → `inductive ModeVal`)
- Boolean and bounded-nat variables
- `case...esac` → nested `if-then-else`
- `DEFINE` inlining
- Nondeterministic inputs (existentially quantified)
- Nondeterministic choice (`{a, b}` → disjunction)
- `next(x)` cross-variable references
- `INVARSPEC` → `sorry`-stubbed theorems (real proofs go in a sibling
  `*Proofs.lean` file)

Tested on `counter.smv`, `traffic_light.smv`, `mutex.smv`, `cellular3.smv`,
`cellular_mc_2x2.smv`.

## Cellular Flows formalization

The [`VerifDemo/CellularFlows/`](VerifDemo/CellularFlows/) directory contains
the full formalization of the TCS 2015 paper. See the header of
[`Defs.lean`](VerifDemo/CellularFlows/Defs.lean) for the authoritative
paper-to-Lean correspondence table (every Invariant, Lemma, Corollary,
Theorem, and Assumption maps to a named Lean theorem or axiom with its
paper section).

Key results covered (all with zero `sorry`):

- **Theorem 1 (Safety)** — axiom-free discrete form (`safety_discrete`),
  plus two continuous bridges (Nat metric / Mathlib ℝ²) requiring an
  explicit `ContinuousReachable` hypothesis
- **Theorem 2 (Liveness)** — `liveness_theorem`, scoped by an explicit
  `IsFairExecution` predicate (Paper Assumption 4 formalized)
- **Lemma 5** — no signal cycles (`noSignalCycle2_invariant`)
- **Lemma 6 and Corollary 7** — route/next convergence, proved in both
  1D (`route_convergence`, `next_convergence`) and 2D
  (`mc_route_convergence`, `mc_next_convergence`)
- **Self-stabilization from arbitrary state** — 1D (`route_self_stabilizes`)
  and 2D (`mc_route_self_stabilizes`) versions of the paper's
  "within 2Δ rounds of last fail" claim
- **Corollaries 8-9** — path/pint stabilization with explicit gossip
  update rule (proved, not axiomatized)
- **Lemma 10** — signal respects lock at color-shared cells
- **Lemma 11** — lock acquisition, scoped by `IsLockFair`
- **Lemma 13** — entities reach target (derived from `liveness_theorem`)
- **Invariants 2, 3** — entity disjoint, single color per cell
- Explicit paper-notation definitions: `NEPrev`, `SC`, `lcs`, entity graph
  `V_E`/`E_E`, routing graph `V_R`/`E_R`, `CellFlowsParameters`
- Parametric `CellTopology` typeclass with 1D-line, 2D-grid, and
  complete-graph instances — demonstrates generality to arbitrary
  tessellations

Finite-instance instantiations verified with both NuXMV and Lean:
- `models/cellular3.smv` — 3-cell single-color line
- `models/cellular_mc_2x2.smv` — 2×2 grid with 2 colors, exercises Lock

Soundness discipline: every active axiom (13 total; 3 superseded, 10 active)
is scoped to a reachability or fairness hypothesis. See `PLAN_V03.md` and
`VerifDemo/CellularFlows/TODO.md` for remaining optional gaps and
paper-ambiguity documentation.

## Key files

| File | Purpose |
|------|---------|
| `VerifDemo/DiscreteMath.lean` | Subset transitivity, De Morgan, distributivity |
| `VerifDemo/TransitionSystem.lean` | `TransitionSystem`, `Reachable`, `InductiveInvariant`, k-induction, ranking functions |
| `VerifDemo/NuXMV/Counter{,Proofs}.lean` | Generated + manual proofs of counter invariants |
| `VerifDemo/NuXMV/Mutex{,Proofs}.lean` | 2-process Peterson mutex |
| `VerifDemo/NuXMV/Cellular3{,Proofs}.lean` | 3-cell line cellular flows instance |
| `VerifDemo/NuXMV/CellularMC2x2{,Proofs}.lean` | 2×2 multi-color instance with lock mutex |
| `VerifDemo/Parametric/NMutex.lean` | **Parametric** N-process mutex, mutual exclusion for all N |
| `VerifDemo/CellularFlows/` | Full 6,000-line TCS 2015 formalization (see Defs.lean header) |
| `VerifDemo/ProgramVerif/Imp.lean` | IMP AST, big-step semantics, Hoare logic |
| `VerifDemo/ProgramVerif/Examples.lean` | Verified swap and counting-loop programs |
| `VerifDemo/ProgramVerif/Sort.lean` | **Insertion sort** — verified correct for all lists |
| `CLAUDE.md` | Architecture and proof patterns reference for Claude Code |
| `PLAN_V03.md` | Remaining optional extensions, prioritized |
| `slides/index.html` | Reveal.js slide deck for the 30-minute demo |

## CI

`.github/workflows/lean_action_ci.yml` runs `lake build` on push/PR via
`leanprover/lean-action@v1`.
