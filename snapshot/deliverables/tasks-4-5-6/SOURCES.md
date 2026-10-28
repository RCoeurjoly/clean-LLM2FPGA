# Source inventory for Tasks 6, 4, and 5

Destination: LLM2FPGA branch deliverables/tasks_6_4_5, based on main at
b6dc8ab. Donor: compiler-lab-llm2fpga branch codex/task5-v2-scaling at
37b2d6c (including Task 4 at e50b658 and Task 6 at ed9ab4b). This is a
curated source import, not a Git-history merge.

The nested flake pins the donor's Calyx source, kev-gpt model-package source,
Nix EDA packages, and open XC7 toolchain. The physical toolchain pin is
extracted from the donor's task3-main/flake.lock: openXC7
aebe5a3cf24ddd84818fb9a96daa96b8b76c796b and bitstream nixpkgs
6fd329b2adfecb86ae49c1cba89689bd0f229e04. The accepted nextpnr source
is 32e8b798520d90e1130afe95908f04cf52125cf6, with the precise
chipdb/route patch stacks in flake.nix. The old
task3-main model pipelines are not part of this import.

| Source group to import | Required by | Reason |
| --- | --- | --- |
| nix/calyx.nix, nix/hardfloat.nix, pinned tool definitions | Task 6 postpass; Task 5 family compiler | Rebuild the accepted Calyx backend. |
| TinyStories exact-package adapters, frozen selection spec, verifier/test, and reference artifacts | Task 6 postpass and image; Task 4 oracle | Keep exact model and arithmetic inputs and the model-selection authority required by the loader. |
| scripts/pipeline/rebuild_task6_bram_*.py and task6_two_rom_mapper.py | Task 6 postpass, image, synthesis, placement and route | Rebuild the accepted compiler-generated two-ROM design. |
| scripts/pipeline/rom_lutrams_xc7.txt | Task 6 native memory mapping | Immutable LUTRAM descriptor loaded by sibling path in the checkpoint mapper; the synthesis recipe checks it before expensive mapping. |
| rtl/tinystories_1m_ypcb_{clock,bram_production_top}.sv and rtl/tinystories_1m_jtag_csr.sv | Task 6 synthesis; Task 4 board interface | Keep the accepted YPCB glue, not an accelerator substitute. |
| XC7 nextpnr, Yosys and prjxray-db patches used by the accepted recipe | Task 6 bitstream; Task 5 XC7 synthesis | Preserve exact tool behavior and physical configuration. |
| artifacts/task4/full16-board-2026-09-29 and its oracle/checker | Task 4 offline evidence gate | Preserve raw board transactions and three programming logs. |
| nix/task4-board-evidence.nix, scripts/pipeline/verify_task4_board_archive.py, scripts/pipeline/infer_tinystories_generation_jtag.py, tests/test_task4_board_archive.py, and Task 4 interface/closure docs | Task 4 offline evidence gate | Verify all three cold-start raw scans, programming logs, oracle tokens, and configuration identity; keep a negative control for corrupt captures/logs. |
| nix/tinystories-family*.nix, nix/tinystories-fixed-point-family.nix, family model adapters/oracle and scripts/pipeline/build_tinystories_family_*.py | Task 5 package, Calyx/SV and image gates | Use the same shape-parameterized compiler path for all five pinned sizes. |
| scripts/pipeline/simulate_tinystories_family_first_token.py, run_large_tinystories_family_sim.py, scripts/analysis/*family* and patched Slang | Task 5 equivalence, bounded synthesis and matrix gates | Verify block-0/token checkpoints and mapped resources where synthesis completes. |
| artifacts/task5/matrix.json, artifacts/task5/archived-synthesis.json and final results report | Task 5 scaling report | The donor matrix is a historical observation with old store paths; the new matrix uses current Nix identities for built stages, while 28M/33M synthesis stays explicitly archived, failed and resource-null. |
| artifacts/task5/board-*.json | Historical Task 5 report links only | Four byte-identical, donor-committed raw 1M board receipts keep the frozen donor report's links intact. They are not active Nix inputs or a fresh hardware gate for this import. |
| artifacts/reference/tinystories-1m-bram-production-nextpnr.json | Task 5 capacity screen | Preserve the donor's 955-RAMB36E1 XC7K480T capacity receipt; exceeding it rejects the current mapping, while remaining below it is not proof of place-and-route fit. |

The Task 6 `compilerSource` is an explicit file allowlist: its postpass and
static-image builds pass with that closure, and the rebuilt image has 89
banks. The new source-derived Task 6 bitstream has identical FASM features,
configuration frames, and decoded bits to the archived board-tested image;
the raw bitstream header/hash differs. Task 4 evidence is an offline archive
verification, not a fresh board run. The five package, generated-SV,
static-image and first-token RTL builds, the 1M two-run 16-token simulation,
and the 1M/3M/8M XC7 syntheses close the imported source dependencies.
Historical receipts may contain /nix/store paths as observations; active
derivations do not depend on them.

Excluded: DDR3/PCIe experiments, Representative Core diagnostics, superseded
router attempts, old Task 3 model-lowering outputs, general-prompt serving,
and copied kev-gpt accelerator RTL. Root flake.nix and docs/project-plan*
remain unchanged.
