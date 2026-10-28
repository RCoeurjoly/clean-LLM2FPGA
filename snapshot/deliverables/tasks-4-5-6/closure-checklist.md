# Tasks 6/4/5 closure checklist

Repair baseline: `deliverables/tasks_6_4_5` at `ab68b8e3a20bd0599fbbda78b172bd54833a4857`.
Authority: `../../docs/project-plan_v2.org`; it remains unchanged.
User approved the repair plan on 2026-10-09 and explicitly prohibited merging into main.
User clarified acceptance on 2026-10-09: Task 3 overutilization was approximately
140×; Task 6 explores 5–10 strategies to make the model fit, and stops evaluating
new strategies once fit is achieved. The user assures us this scope is accepted;
there is no separate written decision. Treat that assurance as authoritative for
this repair scope, without claiming independent documentary confirmation.

| Obligation | Existing evidence | State | Repair-plan owner |
|---|---|---|---|
| 4a interface specification | Task 4 docs; board wrapper/CSR | satisfied | 8: verify final interface remains consistent |
| 4b top-level SV | rtl/tinystories_1m_ypcb_bram_production_top.sv | satisfied | 8: validate final public interface |
| 4c bitstream or PnR report | archived routed configuration; pinned rebuild graph | repair-required | 3–5, 8: bind final configuration to build |
| 4d programming logs | artifacts/task4/full16-board-2026-09-29 | satisfied | 6 complete (8 tests); 8: retain final-image evidence |
| 4e PyTorch output/comparison/demo | continuation oracle; historical tokens | repair-required | 2, 6, 8: portable CPU gate and raw evidence |
| 5a model inventory | family specifications and matrix | satisfied | 5, 7: retain all five sizes |
| 5b per-size Nix derivations | flake.nix family outputs | repair-required | 5: common core/profile |
| 5c stage status, runtime, peak memory | artifacts/task5/matrix.json | repair-required | 5: measured receipts and failures |
| 5c LUT/FF/BRAM/DSP and available Fmax | matrix and synthesis outputs | repair-required | 5, 7: consistent resource accounting |
| 5c plots and trend fits | incomplete | repair-required | 7 |
| 5c bottleneck and minimized reproducer | brief archived bad_alloc summaries | repair-required | 7 |
| 6a explore/survey 5–10 strategies toward fit | resource_usage_reduction_strategies.org; clarify explored candidates | repair-required | 7: account for actual exploration; no invented evaluations |
| 6b evaluate toward fit, without stubs; stop new evaluations when fit achieved | fitting 1M configuration and controlled ROM ablation | satisfied | 7: preserve evidence and explain stopping point |
| 6c resource/Fmax/runtime/peak-memory deltas and viability | incomplete | repair-required | 7 |
| 6c conclusions including neutral/negative results | strategy report | repair-required | 7: match conclusions to receipts |
| Graph-derived lowering route deviation | model-specific Calyx construction replaces prior graph route | acceptance-required | 1: disclose; do not infer acceptance |
| Task 6 early stop after fit | user assures acceptance on 2026-10-09 | satisfied | 1, 7: record clarification; no further evaluations required |
| Universal PyTorch or other architectures | excluded from agreed family scaling scope | not-required | separate future work |
| DDR3/LiteDRAM and whole-model SMT proof | not Task 4/5/6 closure conditions | not-required | separate future work |

## Audit finding owners

F1 structured generation: 4. F2 shared pipeline: 5. F3 measurements/reports: 5/7.
F4 scope acceptance: 1/7. F5 initialization and F6 memory protocol: 3.
F7 reference and bounds: 2. F8 raw archive validation: 6 (implemented; authentic archive and corruption tests pass). Final validation: 8.

## Frozen evidence identities

These identities describe the pre-repair historical baseline, not new validation.
- `artifacts/reference/tinystories-1m-exact-input-contract.json`: SHA-256 `859fe3095a4842e413ee99466f5dc63d5420d0e890a3dce0cf7a52e3bd2d1d3c`.
- `artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json`: SHA-256 `3e726c8e15945b85eb05c584dbb61e4cdd3a370fa4818d58a04542bc3160e0f6`.
- `artifacts/reference/tinystories-1m-bram-production-nextpnr.json`: SHA-256 `feda343f8764e35dbf270f3d7f2b2af6b845e8c97a501c74cf4bb0dec358d351`.
- `artifacts/task4/full16-board-2026-09-29/hardware/receipt.json`: SHA-256 `bbe28dead359698fe9f88a7d3214d795971ad8733b1682135c4168bab9459e1b`.
- `artifacts/task5/matrix.json`: SHA-256 `41f6f0f1a4be3f31b189cf35d8b851cf5ca9b7b46e9e928405e4e5ae821fdb8a`.
- Package manifest SHA-256: `374171e8c0a06dc2632434965f218cf2fc6c82ee15470c47a958b6b9f5f6ca35`.
- Package weights SHA-256: `caa140a70f824334d626e20819effabb3a56c28f35cc5c84e6f5f174b3f6bf4e`.
- Archived bitstream SHA-256: `bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f`.
- Frames SHA-256: `c78a1ab40fea57c214194b95d989fa5ba5e4a32dbef1ea7a5a1595bde6931617`.
- Decoded bits SHA-256: `2c3b88fc99f69e1914388e14ec3bcb5fcd5359e9c2d35c7e9ea81b88fe31cceb`.
