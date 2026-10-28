# Tasks 6, 4 and 5 curated-import verification

Branch: `deliverables/tasks_6_4_5`, based on LLM2FPGA/main `b6dc8ab`.
Donor: `compiler-lab-llm2fpga` `37b2d6c`. All commands below use the
standalone `deliverables/tasks-4-5-6` flake; none takes an old local store
path or donor checkout as an active input.

| Gate | Current verification | Limit |
| --- | --- | --- |
| Task 6 source rebuild | `#task6-bram-bitstream` rebuilt compiler postpass, 89-bank image, synthesis, legal route and decoded configuration. Sorted FASM features, frames and decoded bits matched the archived accepted image; route overuse and architecture failures were zero. Covered clock paths report 55.08 MHz against the 30 MHz constraint. Accepted mapping: 37,793 LUT, 16,780 FF, 813 RAMB36, 89 RAMB18, 9,688 RAM64M, 190 DSP. | Raw `.bit` header/hash differs; BRAM/DSP internal timing coverage is incomplete. |
| Task 4 board archive | `#task4-board-evidence` passed raw JTAG scan, programming-log, oracle and configuration checks for three independent FPGA SRAM reconfigurations. Each yielded `[11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375, 6151, 284, 711, 2354, 287, 262]` in 66.410, 66.409 and 66.400 seconds. Corrupted token and programming-log negative controls were rejected. | Offline revalidation of historical fixed-prompt hardware observations, not a new board run or a general-prompt CLI. |
| Task 5 package → compiler → image | All five pinned 1M/3M/8M/28M/33M Nix packages, generated SV outputs and static images built from the same family generator. | This alone is not functional or physical validation. |
| Task 5 RTL | All five current first-token RTL runs matched token 11 and the named block-0 checkpoint against their model-specific fixed-point oracles. The separate 1M autonomous-feedback RTL run matched the frozen 16-token continuation twice at 1,827,291,855 cycles per run. | The larger models were checked only through first-token RTL, not 16-token or board inference. |
| Task 5 XC7 synthesis | Current Nix synthesis passed for 1M, 3M and 8M. The matrix records 834, 1,875 and 4,351 BRAM36 respectively against 955 available. The 3M/8M mappings therefore exceed BRAM36 capacity. | Family synthesis does not prove route, timing or board fit. 28M/33M `std::bad_alloc` failures are archived donor evidence with null resource counts and no current synthesis identity. |

The reproducible entry points, from the LLM2FPGA repository root, are:

```sh
nix build --no-link --print-out-paths ./deliverables/tasks-4-5-6#task6-bram-bitstream
nix build --no-link --print-out-paths ./deliverables/tasks-4-5-6#task4-board-evidence
nix build --no-link --print-out-paths ./deliverables/tasks-4-5-6#task5-tinystories-family-stage-matrix
nix build --no-link --print-out-paths ./deliverables/tasks-4-5-6#task5-tinystories-1M-16token-simulation
```

The Task 5 matrix output contains `matrix.json` and `report.md`; its current
stage identities are Nix derivation/output paths, while 28M/33M synthesis is
marked `archived-donor` without a current derivation. Six matrix-semantics
regressions passed in the pinned Nix development shell. The nested flake
evaluated offline, and its active Nix expressions contain no `/home/roland`
or `/nix/store` path dependency. Historical receipts may quote store paths as
observations. Four raw Task 5 1M board receipts are retained byte-for-byte
solely to close links in the frozen donor report; they are neither active Nix
inputs nor a new board run. No PCIe/DDR3 work, copied kev-gpt accelerator RTL,
project-plan edit or root-flake modification is part of this branch.
