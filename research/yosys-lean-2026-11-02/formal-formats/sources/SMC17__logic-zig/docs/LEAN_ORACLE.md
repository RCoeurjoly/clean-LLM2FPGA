# Lean oracle & upstream contribution loop

This document is the contract for issue **#3** (Lean↔Zig fixtures) and **#6**
(upstream Lean/Aristotle contributions).

## Non-fiction

| Claim | Allowed only when |
|-------|-------------------|
| “Lean-checked” | `lake build` under pinned toolchain + no `sorryAx` |
| “Zig matches Lean tables” | Fixture schema replay + mutation test green |
| “Upstream contribution” | Public issue/PR link; status ≠ accepted until merge |

## Fixture schema (v1)

Zig source of truth for many-valued tables:

- Module: `src/manyvalued/fixtures.zig`
- Systems: K3, LP, FDE (via `paraconsistent/lp`), Ł3
- Fields: `neg`, `and_`, `or_`, `designated[]`, `schema_version`

Lean should export or prove the same tables. Differential gate:

1. Generate or check fixture blob with provenance.
2. Zig replays every cell.
3. Mutated fixture must fail Zig test (`mutateAndReject`).

## Aristotle boundary

Generated proofs are **untrusted** until:

1. Human statement diff review
2. Kernel compilation
3. Independent checking
4. Zig differential replay

## Upstream loop (issue #6)

1. Identify missing theorem/checker/docs in an upstream project.
2. Reproduce on upstream default branch.
3. Minimal upstream-native patch + tests.
4. Open upstream issue/PR only per their guide.
5. Track proposed → submitted → reviewed → accepted → released.

Contribution count is not a metric. Accepted technical value is.
