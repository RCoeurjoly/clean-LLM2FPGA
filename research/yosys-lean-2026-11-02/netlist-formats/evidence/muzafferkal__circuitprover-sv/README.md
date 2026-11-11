# circuitprover-sv — CircuitProver for parameterized SystemVerilog

A reimplementation of the flow in *CircuitProver: Agentic Lean 4 Theorem Proving with
Reusable Circuit Proof Library for Hardware Verification* (arXiv 2607.27259), with
**parameterized SystemVerilog** as the front end instead of Chisel/Scala. Designs are
verified for **all** parameter values at once: a deterministic translator produces an
executable Lean 4 model with symbolic widths, an LLM agent formalizes the spec and
proves it, and every accepted proof is distilled into reusable libraries.

Every step is checked by a tool, never by trust:

| Step | Tool evidence |
|---|---|
| SV parses and elaborates at each small width | Verilator `--lint-only -GWIDTH=…` |
| Lean model ≡ hardware | exhaustive **one-step** co-simulation, Verilator vs `lean --run`, every (state, input) pair at WIDTH=1,2,3 |
| Spec ≡ intended behaviour | same exhaustive evaluation of the agent-written spec against the validated model, then frozen by sha256 |
| Proof is real | clean `lake build`, no `sorry/admit/axiom/unsafe/native_decide`, `#print axioms ⊆ {propext, Classical.choice, Quot.sound}`, statement re-checked against the frozen `Correct`, `leanchecker` kernel replay |
| Library lemmas are real | admitted only if the library builds and each new theorem passes the same axiom audit |

## Pipeline

```
design.sv ──sv_parser.py──▶ parsed.json ──sv2lean.py──▶ lean/<Lib>/Model.lean   (frozen)
                                  │                            │
                                  └── cosim_harness.py ◀───────┘   Verilator ≡ Lean, all (state,input) pairs
spec.md + Model.lean ──agent (F_spec)──▶ Spec.lean ──spec_validate.py──▶ frozen (.frozen)
Model + Spec ──agent (F_plan/F_proof), compiles ONLY via prove_round.sh──▶ Proof.lean
Proof.lean ──acceptance gate──▶ accepted ──distill.py──▶ proofs/lib/guidance/*.md  +  lean/CircuitProofLib/*.lean
```

* **Phase I — `tools/sv_parser.py`, `tools/sv2lean.py`.** Source-level translation *before*
  elaboration, so `parameter int WIDTH` stays the Lean term `p.WIDTH`. Emits
  `Params` (with constraints), `Inputs`/`Outputs`/`State` (`BitVec (p.WIDTH)` fields; 1-bit
  signals are `Bool`), an executable `step` (always_ff, nonblocking semantics: reads pre-state),
  `output` (assign network), and `run`. Registers without declaration initializers get **no**
  fabricated `init` — theorems quantify over the initial state.
* **Phase I validation — `tools/cosim_harness.py`.** For deterministic Mealy machines,
  agreement of `output` and `step` on the *entire* (state × input) space at a width implies trace
  equivalence at all depths by induction, so the harness checks exactly that rather than sampling traces.
* **Phase II — `tools/agentic_prover.py`.** `spec`: the agent writes `Spec.lean` (a `Nat`
  reference model plus `Correct (p) : Prop`); `tools/spec_validate.py` evaluates it on every pair
  and freezes it. `prove`: the agent may compile only through `tools/prove_round.sh`, which counts
  rounds (one Lean compilation each), enforces the budget, refuses if a frozen file changed, and logs
  every round for distillation. `accept`: the gate in the table above.
* **Phase III — `tools/distill.py`.** A guidance entry (RTL signature → proof route → debug
  recipes mined from failed rounds) and module-independent lemmas, verified before admission.
* **`lean/CircuitProofLib`.** Std-only (no Mathlib). Includes `refRun_eq_of_step`: one-step
  conformance lifts to `∀ t` trace equivalence — the paper's `Spec ≡ Impl` form, proven once, generically.

## Running

Requirements: Docker, Python 3.11+, and the Claude Code CLI authenticated on the host. Everything
else (Verilator, Yosys/SymbiYosys, Lean 4.29.0) lives in the tool image.

```bash
docker build -t circuitprover:1 docker/               # Verilator + OSS CAD Suite + Lean 4.29.0

# Phase I only — translate a design and validate the model against Verilator (no agent, no cost):
tools/new_task.sh design/examples/updown.sv Updown    # task workspace; then edit tasks/updown/spec.md
python3 tools/cosim_harness.py design/examples/updown.sv --params WIDTH=1,2,3

# One task, all phases (spec → prove → accept → distill):
tools/run_task.sh tasks/updown --params "WIDTH=1,2,3" --budget 40

# One arm of one task (label carries the arm and repeat: full-r1, vanilla-r2, retrieval-r1 …):
tools/ablation.sh tasks/updown retrieval-r1 --k 3

# The whole benchmark, resumable across session-quota windows:
ARMS="full vanilla retrieval" REPEATS=2 tools/experiment.sh --resume
setsid nohup tools/experiment_daemon.sh >/dev/null 2>&1 &   # auto-resumes after each quota reset
touch build/STOP_DAEMON                                     # …and stops it

python3 tools/results_table.py            # per-arm means and ratios;  --raw for every run
python3 tools/retrieve.py tasks/gray -k 3 # what the retrieval arm would select, and why
```
The agent runs on the host (`claude -p`, headless); all Lean and Verilator work runs in the container
via `tools/dk.sh`. Each proving run gets a throwaway `/tmp` workspace containing only its own task
project and the lemma modules its arm may use, so arms cannot see each other's results or notes.

## Results (final, 2026-09-02)

12 tasks × 3 arms × 2 repeats (+ the seed run) = 73 proving runs, **73 accepted**. `full` = every
guidance entry + every lemma module (minus the task's own); `retrieval` = the 3 best-matching entries
by RTL signature + only their lemma modules; `vanilla` = nothing. Each run is a fresh agent session in
a throwaway `/tmp` workspace with a private library package, 40-round budget.
`python3 tools/results_table.py` prints the per-task means; `--raw` lists every run.

| metric (geo-mean of per-task means) | full ÷ vanilla | retrieval ÷ vanilla |
|---|---|---|
| proof rounds | 0.90× (3 tasks lower, 9 ties) | 0.90× (3 lower, 9 ties) |
| agent turns | 1.26× | 1.09× |
| output tokens | 1.22× (split 6/6) | **0.71×** (lower on 8/12) |
| time | 1.19× (split 6/6) | **0.72×** (lower on 8/12) |
| proof LoC | 1.06× | 1.08× |

**Verdict on the paper's claims.** Success is at ceiling (every arm proves everything). The vanilla
agent's first compile failed on 3 tasks (modcounter, addc, satadd) where the library arms never did,
but nearly every run is one compile with this agent, so "rounds halved" has no room to show. The whole
library in context is a net cost. **Retrieval reproduces the paper's direction and size**: −28% time and
tokens versus a library-free agent (paper: −23% time), concentrated on the hardest tasks (fifo_ptrs
267 s vs 694, addc 222 vs 459, satadd 200 vs 375, mirror 275 vs 452). Proofs are not shorter. The gain
comes from the guidance *routes*: retrieval proofs cite library lemmas in only 2–3 of 12 cases.

**Caveats.** n = 2 per cell and runs vary up to 4× between repeats; small one-step/invariant designs;
a hand-made 20-tag retrieval vocabulary. Full tables and analysis: `paper/circuitprover-sv.md` §8.

**Discarded data.** The first vanilla arm was invalid (agents reused the full run's proof from a
directory they could read; kept under `results/_contaminated/`), as was a quota-truncated timer run.

**Operational lessons.** (1) Claude Code keys auto-memory by the enclosing git repo. (2) Anything the
agent can read is available to it — ablation arms must not share a filesystem view. (3) Session
quotas ended three experiments; fast-fail detection, idempotent `--resume` and
`tools/experiment_daemon.sh` (sleeps until the quoted reset) let the 47-run extension finish unattended.

### Evidence log

1. `counter` (WIDTH, DEPTH symbolic): Verilator lint/elab OK at WIDTH=1,2,4,8.
2. `counter` model ≡ Verilator on 8/16/32 (state, input) pairs at WIDTH=1,2,3; likewise
   `accumulator` (16/64/256 pairs, BitVec input) and `updown` (16/32/64 pairs, ternary + subtraction).
3. `counter` spec (agent-written) validated on all 56 pairs, frozen.
4. `counter_correct : ∀ p, Correct p` proven by the agent in **1 round** (18 turns, 170 s);
   `#print axioms` = `[propext]`; `leanchecker` OK. Note: the agent cited the library lemma
   `toNat_add_one` in its `simp` set, but Lean's linter reported it unused (core `BitVec.toNat_add`
   fired first) — genuine lemma reuse is measured in the ablation, not assumed.
5. Distilled `proofs/lib/guidance/counter.md`.
6. Trace-level theorem (the paper's `∀ t` form) derived from the accepted one-step proof via
   `CircuitProofLib.refRun_eq_of_step`; standalone-checkable file
   `proofs/evidence/counter_trace_standalone.lean`, axioms `[propext]`.
7. `// require: MAX <= 2**WIDTH` on `modcounter` becomes `Params.require`; the parameter comparison
   `cnt == MAX-1` translates to `s.cnt == BitVec.ofNat p.WIDTH (p.MAX-1)`; model ≡ Verilator on all 11
   constraint-satisfying (WIDTH, MAX) assignments, 4 violating ones skipped. Regenerating the counter
   model reproduces the frozen file byte-for-byte (deterministic translator).
8. Isolation lesson: Claude Code keys its auto-memory by the enclosing git repo, so agent runs with a
   cwd inside this repo shared notes (a "vanilla" run was observed reading the counter recipe). Agent
   runs now use a fresh cwd under `/tmp`; only results produced after that fix count as an ablation.
9. **Co-simulation found a real RTL bug.** The first `gray.sv` wrote `gr <= (bin + 1) ^ ((bin + 1) >> 1)`;
   Verilator and the Lean model disagreed on exactly one (state, input) pair per wrap-around at every
   width. SystemVerilog evaluates that expression at 32-bit context width because of the unsized `1`,
   so the carry out of `bin + 1` survives the `>> 1` — the RTL violates its own Gray-code invariant at
   wrap. The translator now *refuses* this hazard class (unsized-literal arithmetic feeding a right
   shift, comparison, select, concatenation or division) instead of silently modelling it at WIDTH
   bits; the design was fixed with an explicit WIDTH-bit wire and re-validated.
10. Co-simulation also caught a **translator** bug: in `mirror`, `d <= (d == 0) ? MAX - 1 : d - 1` has a
    parameter (Nat) term in one ternary branch, and the emitter took the result shape from that
    branch, producing `BitVec.ofNat (None) …`. Phase I failed loudly (Lean would not even elaborate
    the model), the emitter was fixed to take the shape from the BitVec branch, and mirror then
    matched Verilator on all 11 valid (WIDTH, MAX) assignments. Lesson recorded in process: a task's
    Phase I result gates its Phase II run.

## Paper and layout

* `paper/circuitprover-sv.md` — the write-up: what was implemented on top of CircuitProver, the
  SystemVerilog front end, Verilator's role, harness, benchmark, results and lessons.
* `design/examples/` — the 13 parameterized SystemVerilog designs; `tasks/<t>/` — one workspace per
  design (design, `spec.md`, `params.txt`, frozen `Model.lean`/`Spec.lean`, accepted `Proof.lean`).
* `tools/` — the pipeline (`sv_parser`, `sv2lean`, `cosim_harness`, `spec_validate`,
  `agentic_prover`, `prove_round.sh`, `distill`, `retrieve`, `ablation.sh`, `experiment.sh`,
  `experiment_daemon.sh`, `results_table.py`); `docker/` — the tool image.
* `lean/CircuitProofLib/` — the accumulated, axiom-audited theorem library; `proofs/lib/guidance/` —
  the guidance library; `proofs/evidence/` — standalone-checkable evidence files.
* `results/<task>/<arm>-r<N>/` — every proving run (proof, metrics, round logs);
  `results/_contaminated/` — discarded runs kept as evidence.

## Benchmark suite

Designed to test the paper's claim where it can actually show: tasks that need more than one round.
Grouped like the paper (arithmetic / control / misc); every design is parameterized in WIDTH and
several carry `// require:` constraints that the proof must use.

| task | what must be proven | why it is harder than the seed tasks |
|---|---|---|
| counter, accumulator, updown, modcounter | one-step register update | seed tasks (all closed in 1 round) |
| pwm | `out ↔ cnt < DUTY` with `DUTY < 2^WIDTH` | parameter comparison → `BitVec.ofNat` round-trip under a bound (a library lemma exists — reuse test) |
| mac | `(acc + a·b) mod 2^WIDTH` | product then sum, nested `%` reasoning |
| addc | WIDTH+1-bit `wide = a + b + cin` **exactly**, `sum`/`cout` as slices | zero-extension casts, `2^(WIDTH+1)` bound, `extractLsb`/`getLsbD` at symbolic index |
| absdiff | `diff = \|a − b\|` | wrap-around subtraction with a case split on `a ≥ b` |
| satadd | `acc = min(a + b, 2^WIDTH − 1)` | wire of width WIDTH+1, bit-select at symbolic index, saturation |
| gray | invariant `gr = bin ^ (bin >> 1)` on all reachable states | **trace-level** statement: induction over time |
| timer | invariant `cnt < PERIOD ∧ (tick → cnt = 0)` | two registers, constraint `PERIOD ≤ 2^WIDTH`, invariant |
| mirror | invariant `u + d = MAX − 1` (as Nat) | two counters in lock-step, wrap-around, constraint |
| fifo_ptrs | invariant `cnt = wr − rd (mod 2^WIDTH)` | three registers, modular ring identity |

Invariant specs (`specInv`, `isReset`) are validated before freezing by an exhaustive **reachability
check** at small widths — a finite-state model check of exactly the property the theorem then states
for all parameters. Run everything with `tools/experiment.sh` (resumable across quota windows).

## Supported SystemVerilog subset (today)

Single module; `parameter int`; `input/output logic` with `[HI:0]` ranges; `always_ff @(posedge clk)`
with `if/else`, `case`, nonblocking assigns; `assign`; operators `+ - * / % & | ^ ~ ! && || == != < <= > >=
<< >> ?:`, bit/part-select with numeric or parameter indices, concatenation; sized/unsized literals.
Not yet: `always_comb`, memories, submodule instantiation, generate loops, signed arithmetic.

## Semantics decisions (deliberate, documented)

* Unsigned, modulo-2^width arithmetic (SV `logic`); width joints made explicit with `.setWidth`.
* Synchronous reset only (async-reset triggers are modeled synchronously — flagged, not silent).
* Uninitialized registers are undefined at power-on: no `init`, theorems start from any `s0`.

## License

Apache-2.0 (see `LICENSE`). The benchmark designs, generated models, proofs and libraries in this
repository are original; the flow follows CircuitProver (arXiv 2607.27259) and Chicala (DAC 2024).
