# PCEQ Lean proof spine

Self-contained Lean 4.22.0 package for the revised PCEQ metatheory and structural ledger checker.
It has no external Lean dependencies.

```sh
lake build
lake build PceqTestSupport   # not a default target; builds the smoke roots
lake test
lake exe pceq-ledger-check Test/fixtures/valid.json
lake exe pceq-merge-check Test/fixtures/merge/accept-partition.json
```

The metatheory is under `Pceq/Metatheory/`; finite and `bv_decide` audit smoke support is under
`Pceq/Vc/`; Tier A–D theorem-oriented AXI4 models are under `Pceq/Axi4/`; and the JSON
model/checker is under `Pceq/Ledger/`. The merge-bundle acceptance boundary of the
speculative goal-decomposition layer is under `Pceq/Merge/`: `Schema.lean` and
`Instantiate.lean` bind each merge rule to the kernel-checked theorem it cites, `Sexp.lean`
re-parses canonical SMT text, `Format.lean` mirrors `crates/pceq-plan/src/export.rs` and adds
the strict (duplicate-key, unknown-key) JSON pass, and `Check.lean` is the decision
procedure. `Test/MetatheorySmoke.lean`, `Test/VcSmoke.lean`, and `Test/Axi4Smoke.lean`
kernel-check the layers, while `Test/fixtures/` contains accept/reject ledger cases.

`Test/fixtures/merge/` is a **shared** corpus, not a Lean one. Every file in it is read by
both acceptance boundaries: `Test/MergeSmoke.lean` here and
`crates/pceq-plan/tests/merge_conformance.rs` on the Rust side. The Rust suite *generates*
the fixtures (`PCEQ_BLESS_FIXTURES=1 cargo test -p pceq-plan --test merge_conformance`), so
the accepted ones are the exact canonical bytes `export.rs` emits, and it fails if the
expectation list in `Test/MergeSmoke.lean` is not exactly the directory listing with the same
verdicts. A fixture checked on only one side is therefore a CI failure, which is the point:
the corpus is where a drift between the two checkers becomes visible.

Trust boundaries and schema details are documented in `../docs/tcb.md` and
`../docs/certificate-format.md`.
