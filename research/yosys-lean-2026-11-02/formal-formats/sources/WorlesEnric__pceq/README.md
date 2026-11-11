# PCEQ

PCEQ is a fail-closed implementation of the revised Proof-Carrying Effect Quotient framework.
The workspace provides executable AXI reference, monitor, environment, recurrence, cover,
certificate, proof-checking, ledger, mutation, simulation, and Lean metatheory components. The
current CLI builds the composed DUT × reference × monitor × environment product and runs its
symbolic VC worklist when proof-eligible correspondence evidence is present. The finite accounting
graph remains a non-authoritative lower-bound projection. Current DUT reports remain `LOWER_BOUND`
until a discovered invariant closes every symbolic VC and independently checked VC6 evidence is
available; see `docs/implementation-status.md` and `docs/risk-disposition.md` before interpreting
results.

The implementation follows the repairs in `docs/pceq_assessment.md`:

- environment legality is stateful and segment-decomposable;
- cycle segmentation is the sound base mode, while transaction mode requires recurrence and
  prefix-extensibility certificates;
- proofs induce overlapping symbolic covers (there is no eager kernel-quotient enumeration);
- every edge carries initiation, consecution, prefix-safety, and post-inclusion obligations;
- all regional obligations are relative to a certified global reachable-state invariant;
- closure and symbolic residue are authoritative; a profile percentage is secondary;
- LLM output is an untrusted search hint and has no path to the ledger.

## Status taxonomy

PCEQ deliberately distinguishes these outcomes:

1. `BUG` — a legal trace reaches the trace-inclusion monitor's `Bad` state.
2. `LOWER_BOUND` — checked edges exist, but input completeness, non-vacuity, recurrence, prefix
   extensibility, post closure, or proof availability is incomplete. Symbolic residue is emitted.
3. `FAST_PATH_CLOSED` — the finite product/VC replay checker and fast proof path accept, but the
   Lean audit has not accepted every obligation.
4. `LEAN_AUDITED_TRACE_INCLUSION` — closure and every VC are audited, but `R ⊨ T` is not yet a
   theorem-qualified release claim.
5. `FULL_COVERAGE` — closure, every VC, the applicable linked `R ⊨ T` proof, and any required
   symmetry lemma pass. Publication as a release additionally requires the separate pinned-tool
   and mutation-campaign workflows; those operational gates are not encoded in this status.

No command promotes a lower status merely because an engine printed “safe.”

## Quick start

Rust 1.89 is pinned. Lean 4 is pinned independently under `lean/`. External formal engines are
optional for the deterministic finite-domain tests and required for their corresponding release
gates; `tools/check-toolchain.sh` explains any missing component.

```sh
tools/check-toolchain.sh --manifest-only
cargo test --workspace --locked
(cd lean && lake build && lake test)

cargo run --locked -p pceq-cli -- build scenarios/axil-reg/scenario.toml
cargo run --locked -p pceq-cli -- prove scenarios/axil-reg/scenario.toml --llm off
cargo run --locked -p pceq-cli -- check scenarios/axil-reg/scenario.toml
cargo run --locked -p pceq-cli -- audit scenarios/axil-reg/scenario.toml --structural-only
cargo run --locked -p pceq-cli -- report scenarios/axil-reg/scenario.toml
cargo run --locked -p pceq-cli -- mutate scenarios/axil-reg/scenario.toml
```

This deterministic path exercises the model-only fallback when Yosys is unavailable, so its
report remains `LOWER_BOUND`. A full `audit` intentionally fails closed until every VC has a
verified, linked Lean audit stamp.

To verify that all external source pins are still fetchable, run
`tools/check-toolchain.sh --verify-sources`. To exercise the M0 adapters after installing their
tools, run:

```sh
cargo run --locked -p pceq-cli -- toolchain --smoke
```

The smoke command invokes rIC3, Pono, and Bitwuzla through their real subprocess adapters on
canonical safe fixtures, writes `.pceq/toolchain-smoke.json`, and fails unless all three results
are parsed as safe. It is diagnostic only and never grants ledger proof credit.

The supported clean-checkout release reproduction is the pinned x86_64 Linux container:

```sh
manifest_sha256=$(shasum -a 256 tools/versions.lock | cut -d' ' -f1)
docker build --platform linux/amd64 --pull=false -f tools/Dockerfile \
  --build-arg EXPECTED_MANIFEST_SHA256="$manifest_sha256" \
  -t pceq-formal .
docker run --rm --platform linux/amd64 pceq-formal
```

The `release-formal-toolchain` workflow is the reviewer gate. It runs as UID 10001, fingerprints
every executable, requires real adapter smoke and RTL↔BTOR2 correspondence, and performs two clean
`axil-reg` build/prove/check/report runs with Yosys and correspondence required. It externally
parses every emitted BTOR2 file, compares a fixed content-addressed artifact set, and exports the
two trees, digest manifests, logs, fingerprints, and `reviewer-instructions.txt`. Reviewers must
reject the release if either run fails, any required evidence is missing, or the digest manifests
differ. The timestamped Ubuntu package snapshot and immutable source revisions make inputs pinned;
matching artifact digests, rather than an image tag, are the reproduction criterion.

Artifacts are written under `.pceq/<scenario>/`: canonical product JSON, BTOR2, certificates,
SQLite ledger, canonical ledger JSON, witnesses, and layered Markdown/JSON reports.

## Workspace

The 16 Rust crates mirror the implementation specification. The most important dependency
boundary is that `pceq-llm` can propose inputs to proof search, while `pceq-cert` and
`pceq-ledger` never depend on an LLM result. The Lean package proves the generic composition and
closure theorems and provides the audited ledger/checking spine.

The AXI reference-model feature tiers are:

- A: AXI4-Lite handshakes, data, and response semantics;
- B: INCR bursts, sizes, LAST, aligned narrow lanes, and the 4 KiB rule;
- C: multiple IDs, per-ID ordering, FIXED/WRAP, and outstanding maps;
- D: unaligned narrow lanes, exclusives/EXOKAY, and QoS/REGION/CACHE metadata.

Every scenario and report names its tier and theorem set. A feature absent from that declaration
cannot silently receive coverage credit.

## Security and trust

External engines, model-checker invariants, solver search, and LLM proposals are untrusted.
Commands are invoked without a shell; provider secrets are redacted; caches and certificates are
content-addressed and atomically written. The current trust boundary and validation activities
are enumerated in `docs/tcb.md`; unresolved and mitigated risks are tracked in
`docs/risk-disposition.md`.
