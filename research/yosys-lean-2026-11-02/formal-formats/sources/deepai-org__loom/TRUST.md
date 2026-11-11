# Trust and claim audit

This document is the adversarial reading guide for Loom's claims. The
authoritative release theorem and trusted list are in [`TCB.md`](TCB.md); the
commands are in [`REPRODUCING.md`](REPRODUCING.md); the current gate state is
in [`STATUS.md`](STATUS.md). This page avoids duplicating those inventories.

## Property adequacy

### Timing and observation

LNP64-µ T5 is architectural noninterference over a projected observation
(`regs`, `pc`, `run`, and `cause`) after destuttering. It deliberately erases
when events occur. It is therefore not a timing-channel or power-channel
theorem.

T5 is conditional. The compared manifests and domain must satisfy the exact
`Isolated`, `TopPriority`, `AgreeOn`, code-locality, slot, and W^X-disjointness
premises in `Machines/Lnp64u/Theorems/T5.lean`. The repository includes
concrete premise witnesses, but no summary should shorten the result to
“LNP64-µ provides isolation” without those conditions.

### Progress

T6 and T7 are closed-machine scheduling theorems under explicit strict or
non-strict schedulability and budget hypotheses. They do not establish useful
instruction retirement under arbitrary external memory backpressure,
interrupt storms, clock gating, repeated reset, debug intervention, or
unmodeled exception loops.

The proofs account for modeled issue, countdown, retirement, refill, gate,
and Mover events. A platform must separately say how interrupts, cache/TLB
misses, bus stalls, flushes, and context-switch overhead are charged. Otherwise
the platform can introduce denial of service without contradicting T5 safety.

### Reset and two-state logic

The formal machine starts from `Manifest.initState` or the compiled module's
modeled reset state. This assumes that every proof-visible state element has
the intended value. Electrical reset networks, clock/PLL sequencing, scan,
retention, and partially reset peripherals are not proved.

Lean and the µVerilog semantics use two-state bit vectors. The release RTL
hygiene script rejects major X/Z constructs and incomplete reset/images, but
is a text-level defense, not a complete theorem about Verilog's four-state
semantics or every synthesis don't-care optimization.

### Closed-machine scope

The security proofs cover state changes represented in the model. External
DMA, interrupts, debug, buses, MMU/IOMMU policy, coherency, and peripherals
cannot be assumed safe merely because they connect to a verified core. They
must be excluded by integration policy or modeled through ports with explicit
contracts.

Fault behavior has the same rule. The model gives deterministic outcomes to
modeled illegal instructions and authority failures. An SoC claim must account
for every additional fault, trap, interrupt, and debug source.

## Proof and artifact chain

| Link | What is established | Remaining boundary |
|---|---|---|
| LNP64-µ model and T1–T9 | Kernel-checked declarations with explicit hypotheses | Adequacy of definitions and external review |
| ISS to EDSL core | Unbounded simulation from reset for the modeled machine | Platform behavior absent from both sides |
| EDSL to µVerilog module semantics | Generic compiler-correctness theorems | Concrete text interpretation |
| Concrete SSA witness | Complete declarative denotation as the reference compilation | Generator may fail, but acceptance is kernel-checked |
| Renderer to theorem bytes | Kernel equality over bounded rope leaves | Host file must be associated externally |
| Theorem bytes to RTL files | Exact external `cmp` binder | Correctness of the small binder and host execution |
| RTL text to Verilog behavior | Explicit concrete-SSA adequacy assumption | Verilog tool semantics and four-state effects |
| RTL to a synthesized or mapped artifact | No current Loom equivalence bridge | Synthesis, conversion, target-library semantics, exclusions, and tool behavior |
| Mapped artifact to bitstream/silicon | External implementation evidence | P&R, configuration, timing, reset, CDC physics, analog and side channels |

The release theorem stops before the text-tool row. Passing lower rows does
not enlarge the theorem above them.

## Lean-side controls and residual risk

`lake exe audit` walks compiled declarations and enforces:

- only the three standard axioms, plus two named project declarations that
  expose the legacy µVerilog boundary, in permitted theorem closures;
- no disallowed `sorry` outside theorem/WIP policy;
- no `native_decide` or trusted-compiler theorem dependency;
- no imports from `Machines` or `Tools` into `Loom`; and
- an exact whitelist for unsafe declarations and `implemented_by`, with no
  source `partial` or `extern`.

The audit is useful defense in depth, not a kernel axiom. A bug in its report
cannot cause Lean to accept a bad proof, but could hide policy drift. The final
release declaration and raw axiom closure remain independently inspectable.

Executable replacements currently accelerate compiler/printer traversals,
`Design.toProgram`, memory-read diagnostics, neutral-import DAG lowering/read
validation, and the component graph's Kahn topological-order proposal. Import
memoization is keyed only by pointer identity and retains transparent
reference definitions; the specialized import emission path validates reads
on the compact neutral DAG before constructing the dependent expression DAG.
The Kahn result is an untrusted proposal checked by the
transparent `topologicalOrderCheckB`; only the checker's soundness theorem
certifies acyclicity. The release construction likewise checks candidate
witnesses against reference definitions, so these replacements are not hidden
premises of `verifiedReleases`.

Mathlib adds proof code but not a second proof kernel: its declarations are
checked by Lean. CaDiCaL and other solvers are untrusted proposal engines where
their UNSAT answers are accompanied by LRAT certificates checked by the proved
checker.

The separate `checker/` project is an independent **LRAT** implementation for
cross-validation. It is not an independent Lean kernel or export checker and
has no soundness theorem of its own.

## Synthesis, memories, and CDC

Loom currently makes no post-synthesis equivalence claim. Synthesis,
tool-specific conversion, mapped-cell semantics, and physical implementation
are external evidence. A future logical-equivalence boundary must consume a
technology-neutral graph rather than embed a synthesis tool or target library.

Memory target profiles and `syncReadOkB` are prevention/diagnostic mechanisms.
They predict whether a design shape fits a declared technology; actual mapping
and reset-image delivery still need downstream checking. SRAM initialization
is especially target-sensitive.

Board CDC proofs model the first synchronizer sample adversarially and prove
the digital toggle protocol for all Boolean resolutions. They do not prove a
flop resolves before the next edge, its MTBF, placement constraints, or any
analog fact. Those are explicit physical assumptions.

The generic multiclock layer has the same categorical split, documented in
[`MULTICLOCK_BOUNDARY.md`](MULTICLOCK_BOUNDARY.md). Gray adjacency—including
finite-width wrap—and its connection to executable FIFO pointer steps are
machine-checked. Sampled-pointer validity is stated over unbounded source
generation history, and the finite-ring FIFO proves that every simultaneous
successful storage read/write uses distinct addresses. The parametric storage
composition theorem consumes only `AsyncQueueStorage`; optional RAMB and ASIC
bindings live under `Evidence/Targets` and each expose one named leaf
assumption rather than entering generic Loom. The certified portable path does
not use the specialized raw toggle/Gray SystemVerilog renderers: controllers,
synchronizers, flag logic, and register-bank storage are ordinary compiled
Designs, and the remaining wrapper is structural wiring whose literal emitted
RTL value is carried by `verifiedReleases`. The raw renderers remain explicitly
unverified evidence and cannot inhabit that certified artifact type.
Metastability, resolution time, MTBF, CDC placement/routing, downstream
constraint application, and physical storage-macro behavior remain external
physical assumptions or evidence. An Icarus run is only a syntax/elaboration
and idealized-execution smoke test; it does not support the refinement or
metastability claims.

## Current hardware evidence

The current LNP64mini integration head has an accepted dual-core NetBSD board
run: ping completes 4/4, `uname` and an echo reply traverse gate 1/domain 1,
and the shmif driver uses the domain-2 path. The bitstream uses the validated
LUT-mapped direct multiplier because the installed openXC7 0.8.2 cannot pack
the inferred DSP48 terminal cascade output. These are observations of one
accounted artifact and workload, not proofs of the board wrapper, synthesis,
place and route, bitstream, PS7, DDR, Ethernet, CDC, or arbitrary executions.
See [`STATUS.md`](STATUS.md) and `Machines/Lnp64mini/EXTEND_SPEC.md`.

## Review priorities

The most valuable independent review is:

1. statement review of `Wf`, `Acyclic`, T5's observation and premises, and
   T6/T7 scheduling assumptions;
2. clean-clone Tier A reproduction without accepting cached objects;
3. adversarial review of the tiny host-file binder and concrete-SSA boundary;
4. review of any external synthesis and logical-conversion evidence;
5. explicit SoC contracts for reset, DMA, interrupts, debug, memory stalls,
   and fault routing; and
6. a complete board acceptance run on the release commit.

The right publication wording is: the kernel proves the stated model and
artifact relations; exact host bytes are bound by one disclosed comparison;
tool, integration, and physical claims remain conditional and separately
corroborated.
