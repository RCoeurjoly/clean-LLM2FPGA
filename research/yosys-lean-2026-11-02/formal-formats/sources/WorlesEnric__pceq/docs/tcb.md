# PCEQ trusted computing base

Status: Lean proof-spine implementation, 2026-07-11.

This document states exactly what the current Lean package proves, what its executable ledger
checker checks, and what remains trusted or outside the implemented boundary. It intentionally
distinguishes a structurally complete ledger from a semantic hardware coverage claim.

## Implemented trusted spine

The package in `lean/` is pinned by `lean/lean-toolchain` to
`leanprover/lean4:v4.22.0`. It has no third-party Lean dependencies and does not use mathlib.

The following results are kernel-checked:

- `Pceq.env_append` and `Pceq.env_decompose`: carrying the assumption-automaton state in the
  product makes environment legality segmentable by construction.
- `Pceq.VCBundle.toEdgeCertificate`: VC1 initiation, VC2 consecution, VC3 prefix safety, and VC4
  post inclusion imply the semantic safety-and-post contract of a proof-carrying edge, provided
  the within-segment model-reflection obligations are proved.
- `Pceq.traceSafetyComposition` and `Pceq.closedGraphFullCoverage`: a finite graph with input
  completeness, nonblocking nodes, checked safe/post edges, invariant preservation, initial
  coverage, and closure proves every legal finite segmented trace safe.
- `Pceq.coverageAccountingSound`: every point counted through a full certificate path is safe.
  `Pceq.closedGraphCoverageComplete` proves the converse for legal traces under the closed-graph
  hypotheses.
- `Pceq.GlobalInvariant.reachable`, `Pceq.EdgeCertificate.relativize`, and
  `Pceq.EdgeCertificate.refine`: reachable states remain inside a certified invariant, and
  certificates survive invariant intersection and skeleton splitting when post regions are
  soundly re-expressed.
- `Pceq.Automorphism.traceSafe_iff`: renaming earns coverage credit only through an automorphism
  preserving steps, `Bad`, and observations.
- `Pceq.relativeExistence`: a finite, observation/Bad-compatible safe simulation quotient yields
  a finite certificate-complete cover.
- `Pceq.Segmentation.complete`, `Pceq.partialSegmentSafe`, and
  `Pceq.RecurrenceCertificate`: exact reconstruction, prefix extensibility, bounded cut
  recurrence, and nonblocking are explicit proof obligations rather than implicit assumptions.
- `Pceq.Ledger.check_sound` and `Pceq.Ledger.accepted_implies_fullCoverageShape`: an accepted JSON
  ledger has unique identities and artifact references, closed initial/source/target identifiers,
  audited VC0/VC1–VC6/VC8 references, and at least one outgoing edge per node.
- `Pceq.Vc.Finite`: an executable finite VC1–VC4 checker with per-obligation soundness theorems;
  `Pceq.Vc.Bv` contains kernel-checked `bv_decide` proofs for representative bit-vector
  transition, mask, beat, and invariant obligations.
- `Pceq.Axi4.reference_model_meets_T`: a compact Tier A–D theorem-oriented reference model proves
  handshake stability, matching/no-duplication, per-ID ordering, burst/4-KiB/FIXED/WRAP facts,
  data/response rules, exclusives, and metadata passthrough.

There are no `sorry`, `admit`, custom axioms, or opaque theorem placeholders in the package.

## Trusted items

For the implemented proof spine itself, the trusted base is:

1. The Lean 4.22.0 kernel and the semantics of Lean's core/standard library.
2. The checked PCEQ definitions: the chosen concrete transition relation, `Bad`, observations,
   segment semantics, environment monitor, region meanings, and model-reflection obligations.
3. For an executable ledger-checker verdict, the generated executable, Lean runtime, filesystem,
   and JSON parser. A reviewer can avoid trusting the executable's control flow by importing the
   parsed ledger as Lean data and applying `check_sound` in a kernel-checked audit.
4. The Rust orchestration that canonicalizes and hashes stored artifacts, invokes Lean, and
   stages the exact audit transaction. The Rust schema-4 checker reopens proof/verdict/audit
   objects, rerenders every stored ground VC byte-for-byte, and checks checker fingerprints. The
   smaller Lean projection checker still treats its imported content identifiers as opaque.

For a future RTL coverage claim, the following modeling/correspondence items additionally enter
the TCB unless separately certified:

- RTL-to-transition-system translation and product composition;
- reference-model semantics and the proof that `R ⊨ T`;
- the observation projection and theorem set;
- the assumption automaton and its connection to the deployed environment;
- the raw-cycle-to-segment reflection fields used by `EdgeProblem`;
- any generated Lean definitions and their correspondence to the BTOR2/SMT model.

## Explicitly untrusted

The metatheory requires no trust in the engines that discover invariants or guards. Model
checkers, SMT search, interpolant/unsat-core extraction, LLM proposals, worklist scheduling, and
Rust-side coverage bookkeeping may all be wrong without invalidating a claim: their outputs earn
credit only after they are represented by checked Lean evidence and an accepted ledger.

Fast-path external proof checkers are also not sufficient for a release claim. The JSON format
requires both `fastChecked` and `leanAudited`; the structural checker rejects an artifact lacking
either stamp. Schema 4 binds those flags to content-addressed proof/verdict objects and an exact
generated Lean module accepted with `native_decide`; duplicate, stale, unchecked, or failed
staged audits roll back. This reconstruction currently covers only PCEQ's emitted ground finite
SMT fragment, not a future symbolic RTL product corpus.

## Claim levels

- `REJECT`: malformed ledger, dangling graph reference, missing outgoing edge, duplicate identity,
  missing artifact, or absent fast/Lean audit stamp.
- `FULL_COVERAGE_SHAPE`: the executable ledger checker accepted all finite structural/linkage
  requirements. This is the strongest claim made by the current executable alone.
- `FULL_COVERAGE`: additionally requires semantic Lean witnesses instantiating the transition
  system, VC reflection, initial certificate, global invariant, segmentation obligations, and
  every edge certificate, after which `closedGraphFullCoverage` applies.

The distinction prevents a forged Boolean audit stamp from becoming a semantic theorem.

## Validation and reproducibility

Run from `lean/`:

```sh
lake build
lake test
lake exe pceq-ledger-check Test/fixtures/valid.json
lake exe pceq-ledger-check Test/fixtures/dangling-target.json  # exits 1
```

The golden suite accepts one closed ledger and rejects dangling-target, stale-audit, and
uncovered-node ledgers. `Test.MetatheorySmoke` also instantiates the closure, VC-soundness,
environment, segmentation, symmetry, and finite-quotient theorems on small models.

## Current limitations

- The included finite and `bv_decide` audits are smoke-sized. Exact per-VC reconstruction supports
  ground Boolean/bit-vector literal formulas up to 64 bits; no hardware-scale array, Alethe/LRAT,
  or bit-blasted symbolic VC corpus is checked in yet.
- Artifact strings are exact content identifiers, but the checker does not yet enforce a BLAKE3
  spelling or recompute file digests.
- The JSON checker establishes structural hypotheses and audit-stamp linkage; semantic VC truth
  remains a separate Lean proof import.
- Recurrence and exact segmentation are formalized generically, but the compact AXI theorem model
  is not a cycle-accurate generated RefIR/RTL correspondence proof.
- The relative-existence theorem assumes a supplied finite safe simulation quotient; it does not
  discover one.
- The scope is safety over finite traces. Liveness, fairness, and hyperproperties are not claimed.
