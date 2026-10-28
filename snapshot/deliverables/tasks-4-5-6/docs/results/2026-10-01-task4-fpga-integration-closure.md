# Task 4: TinyStories-1M FPGA integration and hardware validation

The `.#` Nix commands below run from `deliverables/tasks-4-5-6/`. From the
repository root, use `./deliverables/tasks-4-5-6#` instead.

Task 4 of [project plan v2](../../../../docs/project-plan_v2.org) is complete for its stated
**fixed-input hardware demonstration**. The YPCB executed the full
compiler-generated TinyStories-1M design, not a handwritten accelerator or a
Representative Core. The FPGA received four prompt tokens, generated 16
continuation tokens with its own feedback, and matched the authenticated
fixed-point PyTorch-model reference exactly on three separate SRAM programs.
This closure packages earlier board observations; it does not report a fresh
hardware run on 2026-10-01 or claim general-prompt serving.

## Deliverables against the written task

| Plan item | Delivered evidence |
| --- | --- |
| 4a — Frozen host interface | [64-bit USER1/JTAG interface specification](../tinystories-1m-ypcb-jtag-interface.md), grounded in the [CSR RTL](../../rtl/tinystories_1m_jtag_csr.sv) and [executed host client](../../scripts/pipeline/infer_tinystories_generation_jtag.py). |
| 4b — Synthesizable integrated top | [YPCB production top](../../rtl/tinystories_1m_ypcb_bram_production_top.sv), clock/CSR glue, and compiler-generated `main`. `nix build .#task6-bram-postpass --no-link --print-out-paths` rebuilds generated SV; no kev-gpt accelerator RTL is an implementation input. |
| 4c — Synthesis, PnR, bitstream/usage | `nix build .#task6-bram-bitstream --no-link --print-out-paths` rebuilds the source-derived configuration. The [Task 6 fit audit](2026-09-29-task6-fit-audit.md) gives resource counts, legal-route and timing reports, plus the physical-configuration equivalence between the rebuilt image and board-tested image. |
| 4d — Board bring-up/programming logs | Four unmodified [programming logs and the raw JTAG receipt](../../artifacts/task4/full16-board-2026-09-29/hardware/) are committed: three independent full16 SRAM programs and restoration of the first-token baseline. The archived runner and verifier are preserved with them. |
| 4e — Reference, comparison, demonstration | The [authenticated 16-step fixed-point model oracle](../../artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json), [board acceptance report](2026-09-29-compiler-full16-bram-board-acceptance.md), and the comparison below. `nix build .#task4-board-evidence --no-link --print-out-paths` rechecks raw captured token responses, programming logs, oracle and source-rebuilt configuration. |

The oracle comes from the pinned TinyStories-1M GPT-Neo PyTorch exact adapter:
eight layers, hidden width 64, 16 heads, vocabulary 50,257, symmetric INT8
weights/activations, Q16.16 values, Q8.24 scales and ordered wrapping 64-bit
MAC. It is the declared **fixed-point** reference, not an undisclosed FP32
model. Its canonical artifact SHA-256 is
`6417445c81f082e616a70287649b6902bc05c37f7442a85f72a4542f8127d3ef`.

## Short demonstration and comparison

The recorded host used a Digilent HS3 at 1 MHz to program the accepted
bitstream into FPGA SRAM, wrote and read back prompt
`[7454, 2402, 257, 640]`, issued one START, waited for completion and read
all 16 indexed tokens. It repeated the sequence after three independent SRAM
programming operations, then restored the prior first-token image. The host
did not send generated tokens or model states. Raw scans—not just each run's
`passed` field—are checked by the committed evidence verifier.

| SRAM program | PyTorch-derived reference versus board | Generation/readout time |
| --- | --- | ---: |
| run0 | Exact, 16/16 tokens | 66.410 s |
| run1 | Exact, 16/16 tokens | 66.409 s |
| run2 | Exact, 16/16 tokens | 66.400 s |

The exact output in every run was
`[11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375, 6151, 284,
711, 2354, 287, 262]`. Each inference finished below the 300-second bound;
the separate SRAM programming took about 150.7 seconds per run and is not
hidden inside the reported inference time. The bitstream observed on the board
has SHA-256 `bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f`.
The source-rebuilt `.bit` header has a different hash because it embeds its
path and timestamp, but its sorted FASM features, frames and decoded bits
match the accepted configuration. The committed board-receipt SHA-256 is
`bbe28dead359698fe9f88a7d3214d795971ad8733b1682135c4168bab9459e1b`.

## Limits and provenance

- Native synthesis counted 37,793 LUT1–LUT6, 16,780 FF, 813 RAMB36E1,
  89 RAMB18E1, 9,688 RAM64M and 190 DSP48E1 in the accepted mapping; these
  are cell counts, not a claim about total physical six-LUT equivalents.
- This proves the fixed-input Task 4 board demonstration, not an arbitrary
  prompt CLI, other TinyStories sizes, PCIe, or DDR3 inference. The three runs
  were separate FPGA SRAM reconfigurations, not chassis power cycles.
- The constrained route has zero overused wires and zero architecture failures.
  Covered accelerator-clock paths report 55.08 MHz against 30 MHz, but
  BRAM/DSP internal timing coverage remains incomplete; full timing
  qualification is **not** claimed.
- The implementation and this packaging are LLM-assisted. Kev-gpt supplies
  authenticated model-package data/reference context but no copied accelerator
  RTL. Funding acceptance or reviewer approval is not asserted.
- The archived hardware receipt and logs are immutable observed evidence. The
  Nix checker replays their consistency offline; it does not reprogram the
  board. The archived runner/verifier are retained as historical provenance;
  the committed `task4-board-evidence` target is the portable offline check.
  The accepted full16 board image is preserved and BPI flash was not written
  by the recorded demonstration.

Task 5's TinyStories scaling matrix remains separate work. Task 6's early-stop
fit decision is documented independently and does not substitute for this
Task 4 hardware comparison.
