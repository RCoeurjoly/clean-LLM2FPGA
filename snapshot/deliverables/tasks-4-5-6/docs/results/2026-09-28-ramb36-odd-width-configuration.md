# RAMB36 odd-width configuration correction

## Current board result

The corrected immutable bitstream passes the authenticated two-token YPCB
checkpoint: `[11,612]` after exactly one START, in 6.318203071 seconds.
The recorded transactions contain only the four initial prompt writes;
the host supplies no generated token or intermediate model state.
This is the same compiler-generated accelerator, arithmetic, placement,
routing and 30 MHz constraints that previously returned `[11,1375]`.
Only the 754 required RAMB36 configuration features were added.
This hardware result demonstrates that the missing-mode correction resolves
the observed two-token checkpoint, not merely an isolated writer regression.

The runner finishes successfully after independently programming the accepted
first-token image again and obtaining token 11 in 3.182181594 seconds.
Raw transactions and both programming logs are preserved in
`/nix/store/j2i0j6iqx3m9priivpvvljjy7pi00qwd-compiler-two-token-ramb36-odd-width-board-evidence`.
Its executable verification requires the exact token pair, one START, only
initial prompt writes, the authenticated oracle identity, the runtime bound,
and successful baseline restoration. This validates recorded physical-board
observations; it does not rerun hardware in a Nix sandbox.

Bitstream:
`/nix/store/wjhgmrkrqxxfv38xffyywafj2nfbxw1f-compiler-two-token-ramb36-odd-width-bitstream/fixed.bit`.
SHA-256: `460e083f27341d4deb351972249fe71739aec25eca12bd4192055ba4012973a1`.
Independent bitread decoding confirms all 754 required mode bits enabled.
The isolated conversion derivation uses cached pinned prjxray dependencies;
the broad development-shell attempt described below was stopped when it
began an unrelated nextpnr rebuild.

This is **one** observed two-token run, not three cold-start runs or sixteen
tokens. The checker retains `independent_reconfiguration_verified=false`;
the separate completed SRAM-programming log is the programming evidence.
Full sixteen-token board acceptance, repeatability, PCIe/DDR3
integration and full physical timing qualification remain outstanding.

## Requalification of the earlier two-ROM image

The previously rejected compiler-generated image with **both** `c_fc` and
`c_proj` weight ROMs in BRAM now passes `[11,612]` after one START, in
6.333635330 seconds. The host writes only the four initial prompt tokens.
The runner then restores the accepted first-token SRAM image and obtains
token 11 in 3.202103091 seconds. BPI flash is not written.

The earlier two-ROM image returned `[547,1111]`. It predates patch 0045:
all 29 `c_proj` BRAMs use `READ_WIDTH_A=9`, but the original FASM contains
none of the required RAMB36 macro-mode features. Re-emission through the
already-corrected writer preserves all 2,159,251 original configuration
features, adds exactly 783 required mode features, and removes none.
The additions include all 29 `c_proj` modes. Independent bitread decoding
checks every required physical mode bit before SRAM programming.

No accelerator RTL, model data, arithmetic, placement or routes are rebuilt.
All 101,662 cells' semantic connections and all 263,824 named net aliases
are preserved. The first raw JSON comparison fails because re-emission
renumbers bit IDs and omits unconnected zero-width ports. The successful
comparison resolves numeric IDs through complete net-alias groups and
compares every connected port, parameter, cell attribute and top port.
It does not tolerate changed circuit connections. Complete FASM preservation
separately verifies the original physical configuration, except for the
explicit mode-bit additions.

Durable Nix artifacts:

- Configuration and semantic-connectivity proof:
  `/nix/store/88q3axfz5xlc2xa81fjjyszmz2hfk3vz-compiler-two-rom-configuration-correction-evidence`.
- Corrected bitstream and independent decoding:
  `/nix/store/vxwsxf9l6ycqimihwc1h54mxrwk86frx-compiler-two-rom-corrected-mode-bitstream`.
- Raw board transactions, both programming logs, executed runner, oracle
  and verification:
  `/nix/store/g5mq9724i2p6w8w51rfsq2lalkhmvanp-compiler-two-rom-corrected-mode-board-evidence`.

The board-evidence derivation checks the exact token pair, runtime, one START,
four prompt writes, candidate identity, the recorded 783-mode decoding
preflight, successful
programming status, and baseline restoration. It validates the recorded
hardware execution; it does not rerun hardware in the Nix sandbox.
The legacy two-token simulation fixture and the current sixteen-token
continuation have different identities; their complete two-step prefixes
are checked for equality, and the current oracle is bound to the passing
full-sixteen Nix semantic receipt.

Consequently, the old failure is not evidence that `c_proj` must remain in
LUTRAM. Its BRAM implementation works for this authenticated two-token
checkpoint once the known configuration omission is corrected. This supports
reconsidering the compiler's one-ROM-only mapping policy to relieve physical
implementation pressure. It does **not** prove sixteen-token board inference,
three independent cold starts, or full BRAM/DSP timing qualification.
The historical two-ROM route's covered-path result is 61.83 MHz at the
unchanged 30 MHz constraint; no new routing or timing analysis is claimed.

The full-sixteen on-chip design still needs a legal, constrained route and
board acceptance. The approved late-congestion diagnostic runs independently
on its unchanged placement; this two-ROM experiment does not replace that
control or the full project goal. PCIe/UberDDR3 acceptance remains subsequent
work. These experiments and their documentation are LLM-assisted.

## Evidence and scope

The unchanged compiler-generated two-token YPCB image returns `[11,1375]`,
not the authenticated `[11,612]`. Physical snapshots localize active embedding
product mismatches to parity-lane coefficient bits at odd packed addresses.
The selected initialization bit is present in the mapped parameters, original
FASM, decoded bitfile, and live FPGA. Missing weight initialization is not the
explanation for this selected bit.

[Project X-Ray's RAMB36 documentation](https://f4pga.readthedocs.io/_/downloads/prjxray/en/latest/pdf/)
(PDF page 58) requires a RAMB36-specific mode feature for widths 1 and 9,
in addition to each RAMB18 half's width. The original nextpnr writer emitted
only the half widths. All 754 production RAMB36 `READ_WIDTH_A=9` instances
lacked the required feature, and their original encoded mode bits were zero.
For the sensitized `core.token_weight_codes_packed_b1.mem.0.1`, the missing
feature is `BRAM_L_X26Y150.RAMB36.BRAM36_READ_WIDTH_A_1`, encoded at
frame `0x00400d1b`, word 5, bit 24.

The accepted first-token image's retained frames also have no enabled
RAMB36 odd-width macro features. Its original mapped width inventory is no
longer available, so this does not establish which baseline instances required
them. Passing token 11 is not proof of complete memory-read correctness.

The correction is isolated in `nextpnr-kintex-bram-mode-fix`, layered on the
pinned checkpoint diagnostic. Patch 0045 emits each port's macro feature only
for RAMB36 widths 1 or 9. It changes neither INIT data, compiler RTL, model,
arithmetic, placement, routing, clocks nor constraints. Existing packages and
the accepted image remain unchanged. This work is LLM-assisted.

## Executed red/green regression

`tests/test_nextpnr_bram_mode_cli.py` invokes the actual public nextpnr CLI
with placed RAMB36/RAMB18 fixtures and reads its emitted configuration.
The original executable fails all eight odd-width read/write-port cases
because the required macro features are absent. Even-width and RAMB18
negative controls pass. No mocked writer or source-text grep is involved.

The corrected executable passes both mode tests, including the half-width
checks and absence of unexpected macro features:

```sh
nix build --impure .#checks.x86_64-linux.nextpnr-kintex-bram-mode \
  --no-link --max-jobs 1 --cores 2
```

Tool: `/nix/store/wwmfz02wyiy4gvav7n76jmhnpiyfj3q9-nextpnr-xilinx-kintex-bram-mode-fix-0.8.2`.
Check: `/nix/store/5zdqlcnww6vbzcks43f0dsjmbsdnkkp8-nextpnr-kintex-bram-mode`.
The existing checkpoint public-CLI regressions also complete successfully
against the corrected executable.

The first Nix attempt stopped before compilation because the patch hunk's
declared added-line count was incorrect. The corrected patch builds and runs
the regression; this packaging error is not an accelerator failure.

## Verification limits

The repository-wide command `nix develop --impure -c python -m unittest
discover -s tests -v` was bounded to 180 seconds and exited 124 while executing
`test_full_pinned_loader_shell_bar_fixture`. It has no final suite result;
the repository suite is not claimed green.

Configuration-only full-checkpoint regeneration completes successfully.
All 132,911 cell records (including parameters, BELs, ports and connections)
and all 334,332 net-bit aliases are unchanged. All 3,314,716 sorted routed
wire/PIP/strength triples match. Serialization order differs for 123,279 nets;
no routed resource differs. The old generic route checker initially rejects
the retained writer's single-space encoding for an unrouted alias. The direct
comparison strips whitespace only for empty routes and validates every
nonempty triple; it does not choose substitute PIPs or tolerate route changes.

The complete FASM line-set comparison preserves all 2,774,525 prior entries,
adds exactly the 754 required macro features, and removes none. This entry
count treats an INIT vector assignment as one entry, not 256 expanded bits.
Detailed receipts are `bram-mode-fix/checkpoint-identity.json` and
`bram-mode-fix/fasm-delta.json`; executed diagnostic checkers are retained in
`mapped-cproj-cone/check-fixed-bram-{checkpoint,fasm}.py`.

At diagnostic commit `c1f4e68`, the first direct prjxray invocation stops immediately on a missing `fasm`
Python dependency. Bitstream conversion is now running in the existing
`task3-main` Nix development environment, which supplies the pinned prjxray
Python dependencies. Corrected-bitstream programming and board inference
have not yet been verified. Do not equate fixing the documented configuration
omission with proving the cause of token 1375.

The user explicitly authorizes this diagnostic checkpoint despite incomplete
repository-wide verification and pending hardware revalidation. It is not
two-token, sixteen-token, or full timing acceptance.

Those statements describe the configuration checkpoint before the subsequent
successful board experiment recorded above. The retained diagnostics are under
`.superpowers/sdd/2026-09-09-tinystories-16-token-board/checkpoint-recovery-20260928/`,
particularly `mapped-cproj-cone/` and `bram-mode-fix/`. Physical inference must
pass before this is described as an accelerator correction. Full sixteen-token
board acceptance and PCIe/DDR3 integration remain outstanding.

The pinned Kintex chipdb has no cell-timing tile types; LUT/carry delays fall
back to estimates and BRAM/DSP internal timing remains omitted. Regeneration
without routing does not repair or expand this timing coverage. Neither a
passing tool regression nor functional board success proves full timing
qualification.

## Full sixteen-token two-ROM compiler comparison

After the corrected historical two-ROM image passed the two-token board
checkpoint, an isolated full sixteen-token compiler profile moved only the
immutable `c_proj_weight_codes_i8` ROM from LUTRAM to BRAM. The production
mapping default remains unchanged. No accelerator SystemVerilog was edited
by hand, and no kev-gpt RTL was used.

The compiler regenerated the authenticated Calyx IR and lowered it to
SystemVerilog in 3.015 seconds. Its control output is byte-identical to the
passing sixteen-token simulation source. The candidate differs only in the
projection ROM's primitive selection; the two primitive bodies have identical
behavior after removing their module names and synthesis attributes. All 89
authenticated image files are unchanged. This is source behavioral equivalence,
not a fresh full-model simulation or proof of native memory mapping equivalence.
The user-cancelled second simulation was not restarted.

The bounded native synthesis completed successfully within its 30-minute cap.
The resource gate first rejected the actual old netlist for retaining 5,462
RAM64M cells in `c_proj`, then passed against the actual candidate. It verifies
that every other memory inventory is unchanged and all 108 memory checkpoints
completed.

| Native resource | Previous one-ROM profile | Isolated two-ROM profile |
| --- | ---: | ---: |
| RAMB36E1 | 784 | 813 |
| RAMB18E1 | 89 | 89 |
| RAM64M | 15,150 | 9,688 |
| DSP48E1 | 190 | 190 |

The projection ROM alone changes from 5,462 RAM64M cells to 29 RAMB36E1
cells. These are actual reachable native-cell counts, not a placement-fit or
timing claim. The corrected odd-width BRAM configuration writer remains
required for subsequent bitstream generation.

Durable Nix artifacts:

- Compiler output and source-equivalence witness:
  `/nix/store/dd6kqmnzsz0g1jj53hhgb67lllzhdmas-compiler-full16-equivalent-two-rom-postpass`.
- Authenticated static image:
  `/nix/store/xnbq8in3l32c1klirmpbbn6wah25ns55-compiler-full16-equivalent-two-rom-static-image`.
- Completed native synthesis:
  `/nix/store/b358d2hiyr4yxgqk84kz0cdr85vwvc6f-compiler-full16-equivalent-two-rom-ypcb-synthesis`.
- Passed native resource comparison:
  `/nix/store/fm9bk4ynkh7rzzcd1rdbh5w475dl0q88-compiler-full16-two-rom-native-resource-comparison`.

The candidate subsequently passed native packing in 23.91 seconds and
placement in 241.26 seconds, preserving seed 15, the 30 MHz constraints,
`hpwlScaleY='1'`, and the corrected nextpnr tool. Its packed SLICE_LUTX count
is 108,630, compared with the previous profile's 138,935. These are tool
virtual-cell counts, not datasheet six-input-LUT utilization. BRAM36 packing
uses 813 of the tool's 955 slots. The placement-stage accelerator-clock
estimate is 41.70 MHz, compared with 35.33 MHz previously; neither is a
routed timing result.

Completed pack/place artifact:
`/nix/store/jxx5sx0060drg1skrwjl3byh6sc3nqfh-compiler-full16-two-rom-bounded-pack-place`.
It includes both native logs and the placed checkpoint, and deliberately
does not run routing or emit a FASM file.

The candidate subsequently completed its constrained route in 1,717.42 seconds
(28.62 minutes), within the 90-minute cap, at iteration 34,
with zero overused wires and zero architecture failures. The routed
accelerator-clock report is 55.08 MHz, passing the unchanged 30 MHz target.
This qualifies the covered paths only; omitted BRAM/DSP internal timing is
still not qualified.

Completed route artifact:
`/nix/store/vl799qzyilfyjyiysvz06klrz487h13n-compiler-full16-two-rom-placement-route-90m`.
It retains the exact routed JSON, FASM, native log, deadline observation and
source/provenance receipt. Bitstream conversion and independent physical
BRAM-mode checks subsequently passed, followed by exact sixteen-token board
inference on three independent SRAM reconfigurations. See the
[board acceptance report](2026-09-29-compiler-full16-bram-board-acceptance.md).
The accepted first-token SRAM image was restored and verified afterward. The
separate unchanged-placement late-congestion diagnostic completed as recorded
below; it is not a qualified route. BRAM/DSP timing coverage
remains incomplete, and PCIe/UberDDR3 inference remains a subsequent milestone.

## Late routing-conflict localization

The approved diagnostic replay of the original one-ROM placement completed
after 4,940.80 seconds, within its 5,400-second cap. At iteration 34 it captured
87 overused wires and 174 competing bindings, then deliberately stopped the
tool with exit code 255. The evidence builder verified that every reported
wire has two distinct competing nets and produced no routed JSON or FASM.
This is a successful diagnostic gate, not routing or timing acceptance.

Artifact:
`/nix/store/ii2jh1j3gyjf98l00s641hk7rc2vfgpi-compiler-full16-late-congestion-diagnostic-only`.
Its `router2-conflicts.tsv` contains the actual wires, competing nets, PIPs,
and backend grid coordinates; `route.log` and `receipt.json` retain the native
iterations and stop condition.

`core.c_proj_weight_codes_i8.read_addr[2]` occurs on only one of the 87 shared
wires. The report therefore does not support treating that ROM as the sole
or dominant demonstrated cause of late congestion. Most competing bindings
are in arithmetic and activation-memory logic; 160 of the 174 bindings lie
within backend grid x=200–274, y=200–224. Native pin-fanout reduction alone
does not explain or prove resolution of these physical conflicts.

The two-ROM candidate nevertheless has independently verified resource,
fanout and placement improvements, plus the corrected historical two-token
board checkpoint. Its single bounded route-only trial subsequently passed,
as recorded above, using the production corrected tool, seed 15 and unchanged
30 MHz constraints. No synthesis, packing or placement was repeated and no
further RTL changes were applied. The result supports this mapping as a
useful physical improvement without identifying it as the sole cause of the
old conflicts. The subsequent hardware acceptance is recorded separately;
omitted BRAM/DSP timing coverage remains unresolved.
