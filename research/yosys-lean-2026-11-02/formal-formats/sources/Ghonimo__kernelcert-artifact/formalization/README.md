# `formalization/` — Lean 4 development

The Lean 4 code for *"A Semantic-Level Mechanization of the Witness-Circuit
Soundness Metatheorem for Hardware Model Checking in Lean 4"*. **Current state: sorry-free, with the semantic
layer closed end to end** — from the K-restricted combinational-shape checks, through
the reset-extension construction and the semantic simulation obligations, to
machine-checked safety.

```
cd formalization
lake build                        # toolchain pinned in lean-toolchain (Lean 4.15.0)
lake env lean AxiomsReport.lean   # axiom audit — expect NO sorryAx
```

A CI workflow (`.github/workflows/ci.yml`, at the repository root) runs both gates —
the build with a fail-on-sorry-warning check, and the axiom audit with
positive-evidence greps — on every push.

## What is proved

**Level 1 — the semantic soundness metatheorem.** The five checks are stated as the
semantic simulation obligations a valid witness provides: *existential* Reset ("some
full `W`-reset K-agrees with every constrained `M`-reset") and Transition ("some
`W`-successor matches every constrained `M`-step"), with shared **inputs** identified
via `AgreeIn` (the input half of the AIGER `=`/`MAPPING` convention), plus
Property/Base/Step. `witness_sound` derives `Safe M` from them by a trace-lifting
simulation with a 1-inductive-invariant induction and endpoint transfer. Notably,
Level 1 needs no stratification: the obligations are existential, so the shadow
states are supplied rather than reconstructed.

**Stratification, faithful to the format.** `Stratification` mirrors FMCAD'22
Def. 6, *including its self-loop exemption*: an uninitialized latch (AIGER's
`r_l = l` encoding) is marked `free` and exempted, exactly as the format's
dependency graph drops `r_b ≠ b` self-loops. Both directions of the reset-state
story are proved: **existence** (`resetState_isReset` / `reset_exists` — the
level-recursion construction behind CAV'25 Lemma 1) and **relative determinacy**
(`reset_unique` — reset states agreeing on the uninitialized latches are equal;
global uniqueness is genuinely false for the deployed format, and the strict
special case is `reset_unique_of_grounded`).

**Level 2 — both combinational-to-semantic bridges.** `ResetCheckK` renders the
format's reset check in its exact `R↾K ∧ C ⇒ R'↾K ∧ C'` shape; the bridge
`resetCheckK_implies_obligation` derives the existential reset obligation from it via
the reset-extension construction (`extendFrom`/`extendState`: shared latches pinned
to the model reset, remaining latches assigned level by level — the construction in
CAV'25 Lemma 1's own proof). `TransCheckK`/`transCheckK_implies_obligation`
(axiom-free) handle the transition direction via functionality. The composite
**`witness_sound_K`** — K-restricted checks + stratification ⇒ `Safe M` — is the
semantic rendering of the format's soundness statement.

**Instances (Examples.lean).** Four end-to-end compositions: `toy_safe`
(self-witness); `toy2_safe` (+`toy2_safe_K` through Level 2) with a *private*
auxiliary latch outside `K`; `toy3_safe` (+`toy3_safe_K`) with a real input, a
load-bearing constraint, and a *genuinely strengthened* invariant — the model
property's non-1-inductiveness is itself mechanized; `toy4_safe` with an
*uninitialized* model latch (two reset states; relative determinacy applies).

**Counterexamples (Counterexamples.lean).** The mechanized refutation of an earlier
universal-conclusion form of the Reset/Transition checks: unsatisfiable for any
witness with a private latch. Kept as a negative regression against reintroducing
the universal forms.

**Axioms.** The union across the whole development (exhaustive sweep over all
195 constants in the namespace) is exactly `propext, Quot.sound` — **no classical
axioms at all**: `Classical.choice` appears nowhere, and every definition is
computable (the interface transports `liftInput` and `extendFrom` are bounded
constructive scans over the decidably indexed shared interface). There is no
`sorryAx`, no `native_decide`, no `ofReduceBool`; `safe_iff_no_bad` and the
transition bridge use no axioms at all.

## What is NOT here (future work, below the semantic layer)

The AIG-backed circuit semantics (`SeqCircuit`'s fields are semantic Lean functions
standing in for `Std.Sat.AIG` denotations), the verified check encoders and the
CNF/LRAT discharge (`Pipeline.lean` is a documented stub), a decision procedure for
the syntactic stratification side condition, the trusted AIGER front-end resolving
`=`/`MAPPING` into the shared-interface embeddings, and any empirical evaluation.
No end-to-end kernel-checked certificate-discharge claim is made.

## Layout

| File | Contents | Status |
|---|---|---|
| `KernelCert/Circuit.lean` | `SeqCircuit`, `ConstrainedRun`, `Safe`, `BadTrace`, `safe_iff_no_bad` | proved (axiom-free) |
| `KernelCert/Witness.lean` | `Witness` (shared latches **and inputs**, injectivity), `Agree`/`AgreeIn`, the five obligations | definitions + layering header |
| `KernelCert/Stratification.lean` | format-faithful `Stratification` (self-loop exemption), `resetState`, **`reset_exists`**, **`reset_unique`** (relative) | proved (choice-free) |
| `KernelCert/Metatheorem.lean` | `liftInput` (computable), `agreeIn_liftInput`, **`witness_sound`** | proved (choice-free) |
| `KernelCert/Level2.lean` | `ResetCheckK`/`TransCheckK`, `extendFrom`/`extendState`, **both bridges**, **`witness_sound_K`** | proved |
| `KernelCert/Examples.lean` | `toy_safe`, `toy2_safe(_K)`, `toy3_safe(_K)`, `toy4_safe` + stratification instances | proved |
| `KernelCert/Counterexamples.lean` | refutation of the universal-conclusion check forms | proved |
| `KernelCert/Pipeline.lean` | CNF/LRAT discharge plan | documented stub |
| `AxiomsReport.lean` | axiom audit driver (CI gate) | — |

`grep -rn sorry KernelCert/` returns only prose mentions; `Pipeline.lean` is the
only stub and says so in its header.
