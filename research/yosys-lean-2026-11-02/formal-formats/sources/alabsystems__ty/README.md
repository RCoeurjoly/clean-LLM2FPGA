<div align="center">
  <h1>ty</h1>
  <p><strong>Model-check specifications. Find counterexamples. Check certificates.</strong></p>
  <p>A from-scratch model checker in Rust for <strong>TLA+</strong>, <strong>Petri
  nets</strong>, and <strong>hardware</strong> designs — built toward a
  proof-carrying replacement for TLC.</p>
  <p>
    <a href="#quick-start">Quick start</a> ·
    <a href="#where-ty-stands-today">Status</a> ·
    <a href="#check-proof-artifacts-independently">Check the answers</a> ·
    <a href="#what-ty-checks">What ty checks</a> ·
    <a href="#use-ty">Commands</a> ·
    <a href="#architecture">Architecture</a> ·
    <a href="#soundness-and-scope">Soundness</a> ·
    <a href="MIGRATING.md">Migrating from TLC</a> ·
    <a href="CONTRIBUTING.md">Contributing</a>
  </p>
  <p><em>Pre-1.0 (v0.13): the CLI surface moves fast. Every status claim below
  is backed by a committed artifact in this repository.</em></p>
</div>

## What is ty?

`ty` takes a specification and a property and tells you whether the property can
ever be violated. One binary model-checks **TLA+** specs (with TLC as its
behavioral oracle), runs **Model Checking Contest (MCC)** examinations on
**PNML** Petri nets, and checks **AIGER** and **BTOR2** hardware designs. You
supply the model and the property; `ty` returns one of:

| Answer | Meaning | Artifact |
| --- | --- | --- |
| **holds** | The property holds over every reachable state. | On certifying paths, a re-checkable safety certificate. |
| **counterexample** | The property can be violated. | The exact trace of states that reaches the violation, replayable against the spec. |
| **cannot compute** | `ty` cannot settle it within its methods or limits. | The reason — never a guess. |

`ty` is **soundness-first** by design: an engine commits only a verdict it can
justify, and otherwise abstains or falls back to the interpreter. That is a
commitment, not a guarantee. It is checked continuously against TLC, and that
checking still finds defects; see
[Where ty stands today](#where-ty-stands-today). The frontends share one core:
a parallel explicit-state engine; symbolic engines on the
[`ay`](https://github.com/alabsystems/ay) SAT/SMT solver — bounded model
checking (BMC), k-induction, IC3/PDR, and constrained Horn clauses (CHC);
native compilation of hot paths; and a certifying pipeline that emits
independently re-checkable certificates.

(Naming note: this `ty` is unrelated to
[Astral's `ty`](https://github.com/astral-sh/ty), the Python type checker —
same name, same `ty check` spelling, different universe.)

## Quick start

Build with a current stable Rust toolchain (**1.95 or newer**; the committed
`rust-toolchain.toml` pins the exact compiler, which `rustup` fetches
automatically):

```bash
cargo build --release -p tla-cli    # → target/release/ty
export PATH="$PWD/target/release:$PATH"
```

Expect a real build: the workspace is 383 crates, nearly all vendored (see
[Architecture](#architecture)), and `.cargo/config.toml` caps it at four
parallel jobs to bound peak memory. On Linux the committed config links with
`lld` (`apt install lld` / `dnf install lld`); with no Rust toolchain at all,
`docker build -t ty .` produces the same binaries in an image.

`examples/DieHard.tla` is the Die Hard 3 water-jug puzzle: a 3-gallon and a
5-gallon jug, and the goal of measuring exactly 4 gallons. The spec asserts the
invariant `NotSolved == big # 4` ("the big jug never holds 4 gallons"). That
invariant is *false* — the puzzle is solvable — so model-checking it turns the
solution into a counterexample:

```bash
ty check examples/DieHard.tla --config examples/DieHard.cfg
```

`ty` reports the violation and prints the trace that reaches it, each state
labelled with the action that produced it (output abridged; a real run also
prints a few lines of engine provenance — which engine ran and why):

```
Error: Invariant NotSolved is violated.
State 1: <Initial predicate>
/\ big = 0
/\ small = 0

  ... States 2-6 ...

State 7: <BigToSmall line 23, col 15 to line 27, col 44 of module DieHard>
/\ big = 4
/\ small = 3

  States found: 14
  Count completeness: exhaustive
```

That last state is the solution, and `ty` exits `1`. (The count is the distinct
states explored before the violation halted the search — matching TLC's
early-stop behavior here — not DieHard's full 16-state space.) A property that
*holds* instead reports the states explored and exits `0`. Add `--output json`
for machine-readable results, `--workers N` to parallelize, and
`--require-exhaustive` to make a non-exhaustive run fail (a CI gate).

New to model checking? `ty tutorial` is an interactive walk through the same
ideas — `ty tutorial demo` runs this exact check for you.

## Where ty stands today

`ty` is under active development and is **not a validated drop-in replacement
for TLC**. Check your own models against TLC before relying on a `ty` verdict.
This section states only what a committed artifact in this repository or a
public third-party result supports.

**Correctness.** TLC is the behavioral oracle for explicit-state TLA+.
`tests/tlc_comparison/spec_baseline.json` pins both tools' verdicts and state
counts over the 181-row benchmark corpus — 181 model configurations from the
community [tlaplus/Examples](https://github.com/tlaplus/Examples) tree, pinned
by commit, 141 of them TLC-comparable — and at that pin records 156 matches,
**0 mismatches**, 18 rows where `ty` fails outright, and 7 untested. Separately,
`scripts/tla_differential_fuzz.py` property-tests `ty` against TLC directly, and
that fuzzer is the instrument that finds real defects: its campaigns fixed a
family of successor-enumeration and under-specification bugs, including one
false "no error found" verdict on actions that quantify over primed variables.
It is not finished — it still reports divergences on synthetic *ill-formed*
specs, where TLC raises a runtime "not completely specified" error and `ty`
returns a verdict instead. Every reproducer is pinned as a regression test.

**Speed and memory against TLC.** A number is citable in this project only if a
committed ledger under `reports/perf/` contains the row. Everything measured
before 2026-08-01 was deleted by a `.gitignore` rule before it could be
committed, so **every aggregate percentage this project ever published is
retracted** — do not cite them, and do not expect corrected versions.

The current head-to-head is the 2026-08-26 CPU audit
(`reports/perf/2026-08-26-ty-vs-tlc-cpu-audit/`): 123 eligible corpus rows
raced on one host, both tools at one worker, `ty` with `--no-auto-symmetry` so
the state counts stay comparable, scored on **user+sys CPU seconds** — the
metric that needs no qualified host, since TLC's JVM burns roughly 1.5x its
wall clock in GC and JIT threads while `ty` at `--workers 1` is effectively
single-threaded. 111 of the 123 rows are clean: both tools finished and the
distinct-state counts agree.

- **Memory is the unambiguous result — `ty` is leaner on 111 of 111 clean
  rows**, median ratio **0.117** (about 8.5x less resident memory), worst case
  0.685. There is no clean row where TLC used less.
- **CPU is close on the rows that do real work.** `ty` wins CPU on 100 of the
  111, but that headline is dominated by rows where TLC's ~0.6-0.8 s of fixed
  JVM startup *is* most of its cost. Stratified by how much work TLC actually
  does, the 27 rows where it burns 5 seconds or more have a median speedup of
  **1.36x**, and 10 of those 27 fall below parity.
- The other 12 rows are not wins and are not counted as any: 8 skipped by the
  harness's >140 s baseline rule, 1 `ty` timeout (`EWD998ChanID`, recorded as a
  real `ty` defect), 1 `ty` error, and 1 parity-impossible count mismatch.

That audit ran before four of the same day's fixes landed; its committed
re-measurement turns two of the eleven CPU losses into wins, and the report
records both the losses and the re-measurements.

An earlier campaign, the 2026-08-06 corpus sweep
(`reports/perf/2026-08-06-corpus-sweep/`), measured 139 of the 141 comparable
rows — six interleaved pairs per spec per arm, median within-pair ratio,
production arm, classified by exact distinct-state parity:

| Outcome | Rows |
| --- | --- |
| wins runtime **and** memory | 75 |
| loses runtime only | 5 |
| loses both | 3 |
| loses memory only | 1 |
| no valid comparison | 55 |

That table is superseded on the ground the audit covers — corpus-wide runtime
and memory standing — and three things bound what it may claim anyway. The
sweep's own committed 2026-08-13 correction: its summarizer required equal
distinct-state counts but not equal *generated* work, and at least two of the
75 wins fail that stricter test, so it is a state-parity classification kept
as engineering evidence, not the apples-to-apples result this project's
acceptance standard requires. The wins read narrowly: only 15 of the 75 are
rows where TLC itself runs 5 seconds or longer, the rest being short rows
dominated by TLC's ~0.6 s JVM startup floor. And the unpaired bucket
concentrates in the heavy rows — 29 of the 35 eligible rows whose TLC arm ran
30 s or longer yielded no pair — so the hardest rows were the least measured.
A follow-up re-ran 12 of those at 900-second timeouts
(`reports/perf/2026-08-10-heavy-rows-timeout900/`): 3 more two-axis wins (up
to 45.9x faster at 0.33x memory on `MCInnerSerial`), 2 near-misses
(`dijkstra-mutex` memory by 0.008 of the 0.95 gate, `MCCheckpointCoordination`
runtime by 0.017), 1 loss on both axes, and 6 still unpaired with named
causes.

None of these campaigns ran on a qualified benchmark host — development
machines without CPU isolation or benchmark-grade memory and disk accounting —
so the harness itself records them as non-qualifying: evidence that specific
gaps closed, not a benchmark result, and not a statement about your spec.
Corpus-wide strict superiority over TLC on both axes is **not established** —
see [`benchmarking.md`](benchmarking.md) for the standard and the open
measurement blockers. Don't assume `ty` is faster:
measure it with `ty supremacy compare` (interleaved paired runs with
state-count parity checks). Timing `ty check` by hand needs `--force` — `ty`
memoizes verdicts on disk, so a cached repeat reports a time that is not a
measurement.

**Certified proofs.** The committed capability baseline
(`crates/tla-cli/tests/corpus-sweep-baseline.json`, regenerable with
`ty corpus sweep`) certifies **28 of 181** corpus configurations end-to-end
with a re-checkable certificate at a 45-second budget; 144 decline and 9 time
out. Twenty-seven come from the explicit-state fixpoint lane; the symbolic SMT
lane accounts for exactly one (`HourClock`). The symbolic lane remains the
least proven part of the system. A decline is an honest abstention, never a
verdict.

**Petri nets / MCC.** `ty` competed at the 2026 Model Checking Contest across
**all 13 examinations** — with ITS-Tools, one of only two competing tools to do
so. In five of the six scored categories it placed 3rd of the three or four
tools competing there; in Reachability it placed 4th of 4. The submitted binary
also produced wrong answers: 147 of its 45,041 scored values were incorrect (an
MCC "confidence" score of 99.674%), 98 of them in Reachability.
Official results: <https://mcc.lip6.fr/2026/results.php>. Those soundness
defects were root-caused and fixed after the deadline, and every committed
corpus-scale measurement since records **0 wrong answers** — including a
full-corpus sweep over all 23,361 cells of the 2025 corpus
(`reports/perf/2026-08-25-full-corpus-sweep/`: 1,855 of 1,855 models, 108,078
known values answered correctly, **0 wrong**) and a 2026-inputs run of the
post-deadline engine across all 13 examinations. That is not a blanket zero:
one post-fix wrong answer is on record — the symbolic (CHC) reachability
seeding lane emitted a wrong `FALSE` on `LastZero-PT-N10`, and has been
quarantined default-OFF since 2026-07-23
(`crates/tla-petri/src/symbolic/reachability_seed.rs`). Coverage, not
soundness, is the gap: that same sweep scores **61.5% of the 2025 category
bars**, and it scores that at a 30-second, 8 GB per-cell budget against the
contest's 3600 seconds and 16 GiB — a standing, not a contest prediction. `ty`
computes a fraction of what the category leaders do, and an unsolved query
returns `CANNOT_COMPUTE` rather than a guess — the timeout and memory
watchdogs abstain the same way rather than answer.

**Hardware.** The AIGER and BTOR2 frontends decide **safety only**. A design
whose only properties are `justice`/`fairness` is answered `unknown`, not
`unsat` — the frontend never reports an unchecked liveness property as holding.
No HWMCC comparison is committed.

**Native compilation.** Real, on by default, and narrow: under shipping
defaults only a small minority of the 181 corpus configurations reach a
compiled tier, and the rest run the interpreter. Admission is per action and
fails closed.

**Relative to Apalache.** [Apalache](https://github.com/apalache-mc/apalache)
attacks TLA+ symbolically — SMT-bounded checking of inductive invariants.
`ty`'s center of gravity is the opposite end: exact TLC-parity explicit-state
checking with independently re-checkable certificates, plus the Petri-net and
hardware frontends in the same binary.

## Check proof artifacts independently

`ty`'s verdicts are backed by evidence you can replay with a checker that never
ran the search:

- **A counterexample** is a concrete trace. `ty` replays it step by step against
  the original spec before reporting it; `ty verdict-emit` packages trace and
  spec into a self-contained envelope that `ty verdict-check` re-validates with
  the tree-walking evaluator only — no solver, no BFS. `ty graph` exports a
  trace as DOT, Mermaid, or JSON.
- **A safety verdict** can be certified. `ty certify` emits a re-checkable
  certificate and `ty cert-check` replays it, sharing none of `ty`'s search
  state. An explicit-state fixpoint certificate is re-checked by re-deriving the
  reachable set from the certificate's own embedded spec and re-running a proof
  kernel for the Calculus of Inductive Constructions (CIC); a symbolic
  certificate goes additionally through `ay`'s audited proof checker, which
  re-checks each embedded SMT proof with no re-solve.
- **Cross-check with a second implementation.** `ty cert-check --carcara` also
  re-checks each embedded
  [Alethe](https://verit.gitlabpages.uliege.be/alethe/)-format SMT proof with
  [Carcara](https://github.com/ufmg-smite/carcara), a separately implemented
  checker. You install Carcara yourself — it is not a dependency of `ty`, and
  when absent it never flips a verdict. `ty cert-export` writes the same proofs
  out as `.smt2` + `.alethe` pairs for third parties (symbolic-lane
  certificates only; an explicit-state kernel certificate carries no SMT
  obligations, and `cert-export` says so rather than emitting empty proofs).

Certificate rechecking validates the emitted proof obligations. It is **not** an
independent oracle for TLA+ frontend or successor-enumeration semantics: on the
explicit-state lane the re-check runs `ty`'s own parser and enumerator again, so
what leaves the trust base is the search engine, not the semantics.

```bash
ty certify   examples/Mutex.tla --config examples/Mutex.cfg --out Mutex.cert.json
ty cert-check Mutex.cert.json          # independent re-check
```

`ty prove` proves inductive safety symbolically (IC3/PDR + k-induction over
`ay`), re-checks the emitted certificate on the spot, and corroborates it with a
second first-order kernel (`tla-zenon` tableau prover + `tla-cert` checker) over
a logical abstraction of the spec. Further lanes, each with its own independent
re-checker: `ty refine-certify` / `ty refine-check` (kernel-checked refinement
mappings), `ty certify-liveness` / `ty live-check` (`<>P` under `WF(Next)`, by
well-founded descent on an integer measure), `ty certify-all-n` /
`ty all-n-check` (an invariant for *all* values of a scalar `CONSTANT`, proved
parametrically), and `ty reflect-check` (re-discharges a safety obligation
through the kernel's own reflected evaluator instead of the Rust embedder).

`ty tcb-census` prints the trusted computing base behind any kernel-certified
verdict, so it is never implicit: `ty`'s own recognizer / embedder / quoter
code, measured at build time from the sources the binary was compiled from; the
external proof checkers, reported as `unmeasured` rather than estimated, each
with its pin and the command that measures it; and the kernel prelude's axioms
and trust markers.

## What ty checks

- **TLA+ model checking** — parallel BFS targeting TLC's verdicts and state
  counts. Liveness (tableau + SCC), symmetry reduction, partial-order reduction.
  Many TLC flags have direct mappings, with documented gaps — see
  [`MIGRATING.md`](MIGRATING.md). `ty check --quint` (automatic for
  `.qnt.json`) reads Quint's JSON IR instead of TLA+ source.
- **Native compilation** — TLA+ compiles to bytecode, lowers to **trust-ir** (an
  SSA IR with instruction semantics modeled on LLVM's, no dependency on the LLVM
  project), then Trust-CG JIT-compiles machine code at runtime. Admission is
  conservative and per action; the interpreter is the soundness fallback, and an
  action whose successors cannot be proved total on every path is never compiled.
- **Petri nets / MCC** — all 13 standard MCC examinations (state space, upper
  bounds, the three reachability forms, CTL and LTL in both cardinality and
  fireability, one-safe, stable marking, (quasi-)liveness) directly on PNML
  models, with structural, decision-diagram, symbolic and explicit engines run
  cheapest-first.
- **Hardware model checking** — AIGER (IC3/PDR + BMC + k-induction) and BTOR2
  (bad-state reachability via CHC/SAT) frontends.
- **Tooling** — a language server (`ty lsp`), TLA+→Rust code generation,
  simulation, coverage analysis, counterexample-graph export, and side-by-side
  TLC comparison (`ty supremacy compare`).

## Use ty

```bash
# TLA+
ty check MySpec.tla --config MySpec.cfg               # model-check
ty check MySpec.tla --config MySpec.cfg --output json --workers 4
ty certify MySpec.tla --config MySpec.cfg --out MySpec.cert.json   # re-checkable certificate
ty prove examples/MultiAction.tla                     # inductive proof + independent re-check
                                                      # (declines outside that class)

# Petri nets (Model Checking Contest)
ty mcc   ./model-dir --examination StateSpace
ty petri ./model-dir --examination ReachabilityDeadlock

# Hardware (runnable samples ship in examples/hardware/)
ty aiger examples/hardware/toggle-unsafe.aag    --timeout 60   # bad state reachable (sat)
ty btor2 examples/hardware/counter-unsafe.btor2 --timeout 60   # sat; --witness FILE writes the trace
```

`ty --help` shows the curated workflow groups — author, check, diagnose, prove &
certify, hardware & Petri nets, export, benchmark, toolchain. That is a small
slice of the surface: **`ty commands` prints the full catalog**, including the
advanced and internal tiers, and `ty options list` enumerates every `TY_*`
environment option with its support tier.

The default build ships all of it. Building with
`--no-default-features --features mimalloc` produces a lean explicit-state
checker: it drops the `aiger` and `btor2` subcommands, the TLA+ symbolic lanes
of `ty check` (SMT Init enumeration, BMC, k-induction, IC3/PDR), and the
kernel-checked verdict path of `ty certify` and `ty refine-certify`, which then
decline rather than certify (`ty tcb-census` still prints, reporting the kernel
section as unmeasured). The Petri/MCC engines are not optional and remain in
either build.

## Architecture

`ty` is a multi-crate Rust workspace:

| Layer | Crates | Purpose |
|-------|--------|---------|
| Frontend | `tla-core`, `tla-tir`, `tla-value` | Parser (TLA+ and Quint JSON IR), typed IR (TIR), value types |
| Evaluation | `tla-eval` | Tree-walking interpreter and compiled-path reference evaluator |
| Model checking | `tla-check`, `tla-mc-core`, `tla-backend` | BFS, liveness, symmetry, engine selection |
| Symbolic | `tla-ay` | BMC, IC3/PDR, k-induction, CHC via `ay` |
| Compilation | `tla-trust-cg`, `tla-ir`, `tla-codegen`, `tla-runtime` | Native backend (typed IR → trust-ir lowering), TLA+→Rust codegen |
| Proofs | `tla-zenon`, `tla-cert` | First-order tableau prover, certificate checking |
| Hardware | `tla-aiger`, `tla-btor2` | AIGER/BTOR2 model checking |
| Petri nets | `tla-petri`, `pnml-tools` | MCC examinations, PNML I/O |
| Decision diagrams | `tla-dd`, `tla-bdd`, `tla-mdd` | Native DD engines (Petri lane) |
| Tooling | `tla-lsp`, `tla-cli` | Language server, CLI |

Those are the verification crates. The workspace contains **383 member
crates**; the rest are `tla-*` forks of upstream library crates, vendored
in-tree to keep as much of the dependency tree as practical readable and
patchable here. A full build resolves 1,103 packages, every one pinned by
`Cargo.lock`: those 383, 67 from revision-pinned Git dependencies (`ay`, Clean,
trust-ir, trust-cg, plus two third-party forks), and 653 from crates.io.

## Soundness and scope

- **TLC is the behavioral oracle** for explicit-state TLA+; `ty supremacy
  compare` measures both tools on a given spec (requires TLC and Java).
- **`ty check --tlc-fairness`** is the one-switch equal-work basis for a
  hand-run head-to-head arm: every `ty`-only automatic reduction off, pure BFS,
  single worker, declared `SYMMETRY` applied exactly as TLC applies it. It
  refuses to combine with the flags that would break that basis (partial-order
  reduction, simulation, random walks, and the symbolic lanes). The observed
  work counts still require model-specific verification; the switch is not a
  parity certificate.
- **Symmetry is ignored under liveness checking by default**: the orbit
  quotient — exploring one representative per symmetry class — is unsound for
  temporal properties (TLC applies it anyway and can report wrong verdicts).
  `ty` warns and checks without it; pure-safety checking keeps it.
  `TY_MATCH_DECLARED_SYMMETRY=1` — set automatically by `--tlc-fairness` — opts
  back into TLC's behavior: a loudly warned timing-parity lever whose liveness
  verdict inherits TLC's unsoundness.
- **`ty options list`** enumerates every `TY_*` environment option with a status
  tier and curated help. Prefer CLI flags for user-facing behavior:
  production-tier environment options are supported, while diagnostic and
  test-only tiers carry no stability promise.
- **Advanced paths carry evidence.** The symbolic, hardware, and
  native-compilation paths ship by default, but back results on a given spec
  with parity evidence (the engine agrees with the interpreter on that spec) or
  backend-admission evidence. A failed gate falls back rather than forcing the
  route.
- **The trusted computing base is never implicit** — `ty tcb-census` prints it
  (see [Check proof artifacts independently](#check-proof-artifacts-independently)).

## Development

```bash
cargo test -p tla-check                    # the model-checking core
cargo test -p tla-check --features testing # + the storage/parity/soundness suites
cargo test -p tla-cli                      # the CLI
cargo clippy -p tla-cli --all-targets      # lint the CLI
```

The second line matters: eleven `tla-check` test targets — including
`choose_soundness`, `trust_cg_state_graph_parity`, the disk-backed liveness
backends, and the parallel state-count parity suite — declare
`required-features = ["testing"]`, so no plain `cargo test` invocation builds
them, `cargo test --workspace` included. `tla-trust-cg` has the same trap for
a different reason: 23 of its 24 integration test files are
`#![cfg(feature = "native")]` and the crate has no default features, so they
need `cargo test -p tla-trust-cg --features native`.

`cargo test --workspace` builds all 383 workspace crates and wants substantial
RAM; `.cargo/config.toml` already caps parallelism for that reason. There is no
CI — the gates under `scripts/check_*.sh` are run manually. A fresh clone needs
no sibling `ay`, Clean, `trust-ir`, or `trust-cg` checkout: every first-party
dependency is a revision-pinned Git dependency in `Cargo.toml` and `Cargo.lock`,
so any machine that can reach those repositories resolves the identical lock.

To compare against TLC you also need TLC itself and the TLA+ proof library:

```bash
ty corpus fetch                               # benchmark corpus → ~/tlaplus-examples
ty install-tlc install --with-proof-library   # TLC + CommunityModules + TLAPS library
ty corpus doctor                              # which specs are actually comparable, and why
```

The proof library matters more than it sounds: 25 of the 141 comparable corpus
specs transitively extend `TLAPS` / `FiniteSetTheorems` / `NaturalsInduction`,
and TLC dies in the parser without them. `ty install-tlc proof-library` fetches
[tlapm](https://github.com/tlaplus/tlapm)'s `library/` at a pinned commit,
verified per file by sha256. `ty corpus doctor` then reports, per spec, whether
TLC can parse it, whether exact state-count parity with TLC is even possible
(it is not where TLC applies a declared `SYMMETRY` to a liveness property), and
whether a `ty` measurement exists.

## Contributing and support

Bug reports and patches are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md),
and file issues on this repository's GitHub tracker. The most valuable report is
a spec `ty` gets wrong: a verdict that differs from TLC's is treated as a
soundness bug and takes priority over everything else.

## Acknowledgments

`ty` builds on decades of research in model checking, temporal logic, and formal
verification:

- [TLC](https://github.com/tlaplus/tlaplus) (Yu, Manolios, Lamport; MIT) — reference model checker.
- [TLAPM](https://github.com/tlaplus/tlapm) (Chaudhuri, Doligez, Lamport, Merz; BSD) — TLA+ proof system architecture.
- [Zenon](https://github.com/zenon-prover/zenon) (Bonichon, Delahaye, Doligez; BSD) — first-order tableau prover; `tla-zenon` is a Rust port.
- [rIC3](https://github.com/gipsyh/rIC3) (Yuheng Su; GPL-3.0) — primary reference for `tla-aiger`.
- [IC3ref](https://github.com/arbrad/IC3ref) (Aaron Bradley; MIT) — original IC3 reference.
- [Apalache](https://github.com/apalache-mc/apalache) (Konnov, Kukovec, Tran) — symbolic model checking for TLA+.
- [ay](https://github.com/alabsystems/ay) (Yates; Apache-2.0) — SAT/SMT/CHC backend.

Full bibliography: [`REFERENCES.md`](REFERENCES.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE). Copyright 2026 Andrew Yates.

The vendored `tla-*` forks of upstream library crates under `crates/` retain
their upstream licenses (MIT, Apache-2.0, BSD, and others — see each crate's own
license files).
