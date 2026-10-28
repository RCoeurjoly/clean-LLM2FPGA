# Task 5 TinyStories family: compiler scaling study

Import note (2026-10-05): this is the frozen donor report from commit
`37b2d6c`. Its `/nix/store` paths and checked-in `artifacts/task5/matrix.json`
are historical observations, not active inputs to the new deliverable.
Rebuild the source-derived gates from the nested flake; its stage matrix gives
current Nix identities for completed stages and labels 28M/33M synthesis as
archived failures with no current derivation or mapped resource count. No new
board run is claimed by this import.
Commands using `.#` below run from `deliverables/tasks-4-5-6/`; from the
repository root, use `./deliverables/tasks-4-5-6#` instead.

Status: **scoped compiler-family acceptance complete** as of 2026-10-02. The
earlier package-only ABI diagnostic is superseded by the shared
shape-parameterized Calyx generator. All five generated-RTL first-token gates
pass, and a source-owned 1M bitstream passes YPCB inference. Serial monitored
XC7 synthesis now provides mapped resource counts for 1M, 3M and 8M; 28M and
33M instead exhaust the 8 GiB tool-memory guard during BRAM techmapping.
Cross-size placement, timing and board inference remain unmeasured. The
checked-in `artifacts/task5/matrix.json` mirrors the Nix-generated family-stage
matrix through bounded synthesis, including each stage's Nix derivation and
output path. Its `board` and `route` fields do not include the later,
separately reported 1M physical acceptance.

All five pinned checkpoints have been packaged under the accepted fixed-point
contract and lowered through the same compiler generator to Calyx and
SystemVerilog. Nix also builds their static model images. No kev-gpt or
handwritten accelerator RTL is used for those outputs. The 33M width exposed
a non-power-of-two channel-indexing bug; the compiler now uses exact bounded
modulo for its 768- and 3072-channel memories, while the 1M generated outputs
remain byte-identical to the pre-fix outputs.

On the current branch, `nix build --impure --rebuild --no-link` compared the
fixed-point package, generated-SV and static-image derivations against their
existing store outputs for **each of 1M, 3M, 8M, 28M and 33M**; all fifteen
comparisons passed. This checks the pinned package → Calyx/SV → shared image
chain's Nix output reproducibility; inference and implementation were verified
separately. The separate sixteen-token 1M image also passed the
rebuild comparison. Regenerate the matrix with
`nix build --no-link --print-out-paths .#task5-tinystories-family-stage-matrix`.

| Label | Parameters | Layers | Width | Generated SV/image | First-token RTL | Bounded XC7 synthesis | YPCB |
| --- | ---: | ---: | ---: | --- | --- | --- | --- |
| 1M | 3,745,984 | 8 | 64 | pass | block-0 exact; token 11 at 85,986,222 cycles | deterministic first-token pass: 30,545 LUT, 834 BRAM36, 83 BRAM18, 190 DSP | full16 Nix bitstream: exact board inference; three separate diagnostic SRAM runs also pass |
| 3M | 8,278,400 | 8 | 128 | pass | block-0 exact; token 11 at 246,420,272 cycles | pass: 31,697 LUT, 1,875 BRAM36, 24 BRAM18, 190 DSP; exceeds board BRAM capacity | not tested |
| 8M | 19,702,528 | 8 | 256 | pass | block-0 exact; token 11 at 793,780,526 cycles | pass: 42,605 LUT, 4,351 BRAM36, 69 BRAM18, 190 DSP; exceeds board BRAM capacity | not tested |
| 28M | 51,987,968 | 8 | 512 | pass | block-0 exact; token 11 at 2,794,470,442 cycles | `std::bad_alloc` in BRAM techmap under 8 GiB guard; resources unavailable | not tested |
| 33M | 68,514,048 | 4 | 768 | pass | block-0 exact; token 11 at 3,234,143,987 cycles | `std::bad_alloc` in BRAM techmap under 8 GiB guard; resources unavailable | not tested |

The compiler image manifests declare 32,021,568, 68,301,888, 158,688,320,
414,958,656 and 644,523,072 source bits for 1M through 33M, respectively
(3.82, 8.14, 18.92, 49.47 and 76.83 MiB). As an intentionally generous
screen, the archived XC7K480T nextpnr capacity of 955 RAMB36E1 and 597,200
`SLICE_LUTX` sites would hold at most 73,425,920 bits (8.75 MiB) if **every**
BRAM were filled and **every** LUT site could act as an independent 64-bit
memory. The 8M, 28M and 33M images exceed even that optimistic uncompressed
storage bound by 2.16×, 5.65× and 8.78×. The 3M image is below this generous
bit-count bound but its actual mapped 1,875 BRAM36 cells already exceed the
device's 955. This is not a proof about a different compressed or logic-encoded
implementation. The capacity figures are from
`artifacts/reference/tinystories-1m-bram-production-nextpnr.json`.

The five passing RTL rows use the pinned four-token prompt, compare every
element of the named `block.0.output` checkpoint with the model-specific
fixed-point oracle, and produce the model-specific first token. Their Nix
result receipts resolved from the current branch are, respectively,
`/nix/store/i1d2qbisyj7idb05v11zm33lj8k2w49h-task5-tinystories-1M-generated-rtl-first-token`,
`/nix/store/xbqlcsc4gsq0239yad8pvifgf1hjiv8l-task5-tinystories-3M-generated-rtl-first-token`,
`/nix/store/acr11qca5r2rp7daj8w74vij104iajdi-task5-tinystories-8M-generated-rtl-first-token`,
`/nix/store/7z69r3lmiyjg4d382ph5ym2awwjny8yb-task5-tinystories-28M-generated-rtl-first-token`,
and
`/nix/store/x1vgsinlssglgcxjpi7pv35jgpghng17-task5-tinystories-33M-generated-rtl-first-token`.
The current-branch Nix evaluation found all five outputs already built; it
did not rerun those long simulations. An independent JSON comparison of each
resolved `trace.json` with its Nix-pinned model-specific oracle found exact
prompt IDs, all 64/128/256/512/768 words of `block.0.output`, and first token
11 for 1M/3M/8M/28M/33M. The current-source 3M rerun produced token 11 at
246,420,272 cycles, and `cmp` found its complete `trace.json` byte-identical
to the earlier 3M passing receipt.

The first bounded family-1M XC7 synthesis finished in 2,451 seconds and
mapped 30,545 LUT1–LUT6, 15,011 FF, 834 RAMB36E1, 83 RAMB18E1 and 190
DSP48E1. Its Nix receipt is
`/nix/store/93kf8dpf5z1iak6kqwb4c3cgcq1wifk3-task5-tinystories-1M-bounded-xc7-synthesis`.
This is a resource count below the headline device capacities, **not** a
placement, timing or board-fit result. That first recipe retained Yosys's
variable runtime log and elapsed time inside its output; the corrected,
deterministic-output recipe passed in 2,503 seconds at
`/nix/store/0f5xbkzf1parrdah6c3v5h0cm8fxqifj-task5-tinystories-1M-bounded-xc7-synthesis`.
Its mapped `design.json` is byte-identical to the first synthesis result.
For the **previous** matrix derivations, Nix's build logs record 1,916.94
seconds for the passing 1M synthesis and 3,600.31–3,600.38 seconds for each
3M–33M run stopped by the old one-hour bound. Those larger-model runtimes
were censored; the monitored runs below supersede them. Machine-local wall
times and RSS observations are diagnostics outside the deterministic Nix
outputs.

Before the Slang patch, a bounded 3M synthesis returned Yosys
`ERROR: Exception: std::bad_alloc` after 32.4 seconds under the 8 GiB virtual-memory
limit. The deterministic Nix receipt is
`/nix/store/xpx2np5qiz3y6f1bcmic4acsvmga5c8l-task5-tinystories-3M-bounded-xc7-synthesis`.
This localizes the first synthesis failure to host/tool memory before a mapped
resource report; it neither demonstrates FPGA fit nor establishes a physical
capacity limit. A higher-memory or staged synthesis experiment would be a
separate gate. A bounded replay of the same `read_slang` command and 8 GiB
limit printed `1. Executing SLANG frontend` followed by the same
`std::bad_alloc`, without reaching Yosys hierarchy or any mapping pass. Thus
the first evidenced failure is frontend peak memory, not BRAM packing.
With the same 8 GiB cap, Yosys's native `read_verilog -sv` parsed and
elaborated the exact compiler-generated 1M, 3M, 8M, 28M and 33M images in
4.7, 8.7, 17.3, 40.1 and 53.9 seconds, using 187, 219, 337, 664 and
994 MB peak, respectively. These are bounded frontend probes, **not** full
synthesis or resource counts. A separate full 3M XC7 probe using the native
frontend advanced into Yosys optimization, then hit its 3,600-second bound;
the last observed log line was `OPT_CLEAN` following `OPT_MERGE`, `OPT_SHARE`
and `OPT_DFF` (pass 9.36.200).
The sampled `main` optimization step reported 30,459 cells, **not** a mapped
FPGA resource count. The native frontend removed the initial Slang memory
failure in that diagnostic, but the full 3M mapping remained unqualified.
The production recipe continues to use Slang, not the native frontend.

Keeping Slang as the production frontend, a second bounded 3M diagnostic
raised only the frontend virtual-memory limit from 8 to 10 GiB. The same
`read_slang --no-proc` import still failed with `std::bad_alloc` after 29.6
seconds; measured peak resident memory was 4,523,440 KiB. With the same
source and 10 GiB cap, Slang's AST-only stage passed in 0.24 seconds at
116,948 KiB. With an 8 GiB cap, `read_slang --ignore-initial` plus hierarchy
passed in 1.29 seconds at 264,012 KiB. These probes localize the excessive
frontend allocation to translating the initialized design into Yosys, rather
than parsing its SystemVerilog. `--ignore-initial` drops the model image and
is **not** a valid synthesis workaround; the production recipe remains Slang
with initialization intact; its 3M resource result was unavailable at that
earlier checkpoint.

The pinned Task 5 Slang source was then patched at the evidenced allocation:
for inferred memories, the initial procedure emits the same memory-init
object without also inserting every initialized bit into a per-bit shadow
dictionary. The unpatched real 3M frontend failed under 8 GiB; five sequential
Nix regressions of the patched Slang frontend now pass under the same guard,
retaining exactly 89, 114, 163, 261 and 457 memory-init objects for 1M, 3M,
8M, 28M and 33M. These are **frontend** results, not mapped resource counts
or proof of equivalent initialization data. The previously pinned Slang plugin
remains unchanged outside the Task 5 derivations. The full patched-Slang 1M
YPCB synthesis passed under the same bound with 30,803 LUT, 15,287 FF,
834 BRAM36, 83 BRAM18 and 190 DSP, and its mapped `design.json` is
byte-identical to the pre-patch Slang result. The patched receipt is
`/nix/store/awzb8hcyfzfk5kq0n34ml2djyrncgfg0-task5-tinystories-1m-ypcb-bounded-synthesis`.
This checks the actual 1M
mapping, not 3M–33M mapping or board timing. The current-source Nix
initialized-memory gate also passes for all five sizes, importing the exact
89/114/163/261/457 memory-init objects above. Full patched-Slang synthesis
of each 3M, 8M, 28M and 33M image then reached its 3,600-second bound without
a mapped resource report. Those were timeouts, not evidence of FPGA capacity
or a SystemVerilog frontend rejection. The monitored runs below replace this
timebox-limited conclusion.

The original 2-billion-cycle/two-hour limits were judged too small for 28M
and 33M after 8M required 793,780,526 cycles. Their initial runs were canceled
without a passing result. The replacement Nix runs are bounded at 6/7 billion
cycles and twelve hours, respectively, and emit live cycle progress plus the
first block-0 checkpoint outcome. Both wide-model runs reached an
exact `block.0.output` match against their own fixed-point oracles: 28M at
310,568,096 cycles and 33M at 692,334,722 cycles. They subsequently produced
exact token 11 at 2,794,470,442 and 3,234,143,987 cycles, respectively. The
bounds were limits, not predictions of completion time.

The current-source 8M rerun hit its 7,200-second wall-clock limit while four
CPU-bound model/synthesis jobs shared the host; the prior 8M source revision
had matched block 0 and token 11. This is a timebox failure, not an observed
semantic mismatch. An 8M-only Nix retry now uses the existing monitored runner,
the unchanged 2-billion-cycle limit and a 14,400-second wall-clock cap. Nix
evaluation confirmed the active 28M and 33M derivation identities were
unchanged. The bounded 8M retry passed with exact block-0 and token 11 at
793,780,526 cycles. The old unpatched-Slang bounded 8M and 28M synthesis
receipts separately failed with `std::bad_alloc` before resource reporting;
their patched-Slang frontend regressions passed. Their later monitored
full-synthesis results are reported below.

## Serial monitored synthesis, 2026-10-02

The same pinned Slang/Yosys XC7 synthesis command was rerun **one size at a
time**. The 1M safety ceiling remains 3,600 seconds; 3M–33M have an
eight-hour emergency ceiling, not a one-hour cutoff. The unchanged 8 GiB
virtual-memory limit guards Yosys, and larger sizes stop if free disk space
falls below 10 GiB. The runner emits a Nix-log progress event every minute:
numbered Yosys pass, last output line, elapsed time, process CPU/RSS, and free
disk space. Distinct memory banks and BRAM template identifiers were observed
advancing; CPU activity or log size alone was not treated as convergence.
Variable timings and process-resource observations stay in the Nix build log
rather than the deterministic output receipt. The complete Yosys log is
removed from the output as before. The Nix-generated results and derivations are recorded
in `artifacts/task5/matrix.json`.

| Size | Observed elapsed | Highest sampled RSS | Synthesis outcome |
| --- | ---: | ---: | --- |
| 1M | 1,263 s | 2.3 GiB | pass: 30,545 LUT, 15,011 FF, 834 BRAM36, 83 BRAM18, 190 DSP |
| 3M | 2,252 s | 3.3 GiB | pass: 31,697 LUT, 14,932 FF, 1,875 BRAM36, 24 BRAM18, 190 DSP |
| 8M | 4,314 s | 6.1 GiB | pass: 42,605 LUT, 14,959 FF, 4,351 BRAM36, 69 BRAM18, 190 DSP |
| 28M | 5,316 s | 7.0 GiB | failed: `std::bad_alloc` in BRAM `TECHMAP` (pass 9.33.2); no mapped netlist |
| 33M | 5,932 s | 6.9 GiB | failed: `std::bad_alloc` in BRAM `TECHMAP` (pass 9.33.2); no mapped netlist |

The monitored 1M `design.json` compares byte-for-byte with the previous
accepted family-1M synthesis result, so adding the observer did not change
the mapped design. The 3M and 8M outputs are actual synthesis passes, not
board-fit results: even 3M requires almost twice the YPCB's 955 BRAM36
sites, and 8M requires more than four times that count. No route or board
attempt was made for these over-capacity images. The 28M and 33M failures
identify a **host/tool-memory** limit at the fixed guard, not a physical FPGA
resource count. Their deterministic receipts say `Yosys exited -6: no ERROR
line in log` because the parser recognizes Yosys `ERROR:` lines, whereas the
live Nix logs show the C++ `what(): std::bad_alloc` exception and pass 9.33.2.
The receipts retain failure status; neither failure is reported as a
timeout. These serial-run wall times are diagnostic observations on this
host, not a general synthesis-time scaling law.

A separate Nix derivation now builds the exact TinyStories-1M 16-token family
image from the same Calyx generator. Its source-memory declarations and every
image file are byte-identical to the one-token family image; the generated
Futil and normalized SV also matched the earlier diagnostic build. The Nix-owned
two-run autonomous-feedback RTL simulation now passes at
`/nix/store/0agxcbf2r9fn8dqal0km7wmsiz93cxnj-task5-tinystories-1M-16token-simulation`:
both runs select and expose the exact frozen sixteen tokens in 1,827,291,855
cycles each. Its raw `execution.jsonl` has sixteen ordered selection and
sixteen token-port events per run, with no context mismatch. At each selection,
the harness checks that the selected token is written into the next context
address and that the complete prior context equals the authenticated prompt
and generated continuation. This is generated-RTL evidence, not physical
timing or a Nix-qualified board bitstream.

The previously accepted 1M board baseline remains separate: it used 37,793
LUT1–LUT6 cells, 16,780 FF, 813 RAMB36E1, 89 RAMB18E1, 9,688 RAM64M, and
190 DSP48E1. Covered paths reported 55.08 MHz against a 30 MHz constraint,
with incomplete BRAM/DSP internal timing coverage. See
`docs/results/2026-10-01-task4-fpga-integration-closure.md`. None of those
counts may be extrapolated to the newly generated family artifacts. Full XC7
mapping and candidate fit are separate gates; a correct RTL token is not proof
of board fit, timing closure, or usable board inference.

As a preliminary integration check, the current Nix-built 1M family image and
the existing YPCB clock, JTAG CSR, and production top elaborated together in
Yosys with Slang reading the generated `main` and native Yosys reading the
wrapper. `hierarchy -top tinystories_1m_ypcb_bram_production_top -check`
finished with zero errors under an 8 GiB virtual-memory cap and a 300-second
timebox. The only printed warnings were the native frontend's expected
real-valued MMCM parameters represented as strings. This verifies the source
interface, **not** integrated synthesis, constrained fit, timing, bitstream,
or inference for the Task 5 image.

The Nix-owned 1M YPCB integration now also passes bounded XC7 synthesis with
the same compiler-generated family image, Slang core frontend, existing YPCB
clock/CSR/top, 8 GiB virtual-memory cap and one-hour tool bound. The first
receipt incorrectly counted only the wrapper because its `keep_hierarchy`
attribute retained the mapped `main` module. A red/green hierarchy regression
corrected that accounting; the rebuilt mapped `design.json` is byte-identical
to the first run. The corrected combined resource count is **30,803 LUT,
15,287 FF, 834 RAMB36E1, 83 RAMB18E1 and 190 DSP48E1**. The top has exactly
one compiler `main` instance with the prompt/control/token ports connected.
This is a plausible **synthesis** resource candidate for the one-token image,
not a fit or timing result by itself.

On 2026-10-02, a separate bounded diagnostic built the exact Nix-generated
**sixteen-token** family image into the unchanged YPCB wrapper with patched
Slang/Yosys, then placed, routed and converted it using the accepted 30 MHz,
seed-15 Kintex profile. The SV source matches the Nix image byte-for-byte.
The integrated mapped design contains 32,540 LUT, 15,591 FF, 860 RAMB36E1,
111 RAMB18E1 and 190 DSP48E1. The route ended at zero overuse and zero
architecture failures; covered accelerator-clock paths report 49.34 MHz
against 30 MHz. The resulting SRAM-only bitstream has SHA-256
`9805f7c25c1a6b3a5f33f3f38d67f62255fb314f3a52db9133ba63e3d36f767c`.
The source-owned Nix YPCB synthesis now independently passes with the same
resource counts and one retained compiler `main` instance. Its mapped
`design.json` is byte-identical to the diagnostic mapped design (SHA-256
`49290003dec49e80f76e8a3171c0d526e402687be849abee828232d91eeab6ef`);
the Nix result is
`/nix/store/clzkpv1jkf2r3zjb1a68a1xm4d2qs14w-task5-tinystories-1m-ypcb-bounded-synthesis`.
The source-owned Nix route also passes at
`/nix/store/qn47za2ri3z5b3v8y2l5mx0d3aknj7ka-task5-tinystories-1m-16token-ypcb-route`.
It reports zero overuse and architecture failures, router1 legality, and the
same covered accelerator-clock result of 49.34 MHz against 30 MHz. Its
`routed.fasm` is byte-identical to the board-tested diagnostic FASM (SHA-256
`1b62fbdb8d943d5a35c759944562d9c5d01442ea124d93100b2fba47cb548467`).
The diagnostic bitstream's decoded physical frames contain all 783 required
native odd-width BRAM features, including 29 projection-memory features. This
remains covered-path timing, not complete BRAM/DSP internal timing qualification.

Programmed on YPCB, that diagnostic image generated the complete frozen
continuation `[11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375,
6151, 284, 711, 2354, 287, 262]` after one START with autonomous token
feedback. Three separate SRAM programs of the identical bitstream each passed
the 16-token checker in 61.525, 61.534 and 61.572 seconds, respectively.
The [first raw receipt](../../artifacts/task5/board-diagnostic-2026-10-02.json),
[second raw receipt](../../artifacts/task5/board-diagnostic-2026-10-02-run1.json)
and [third raw receipt](../../artifacts/task5/board-diagnostic-2026-10-02-run2.json)
are preserved byte-for-byte; they contain 2,603, 2,609 and 2,683 JTAG
transactions, respectively. Each contains exactly four host prompt writes,
one START and sixteen token reads, with no host-supplied generated tokens.
Their file SHA-256 values are, in order,
`658b70d7c2acef3c5c09c231906d50c86afac3464c60cd8f3a194e504df096c9`,
`ad35dca6fc89777c24f91428e49a20907b31a2a5920113706febe6209436e5a5`
and `3df760f861dad1b1d5ef560fa4873444d007b55401e4ef77ddef1c37910b045b`.
The checker alone cannot attest to reprogramming, so its
`independent_reconfiguration_verified` field remains `false`; the three SRAM
programming completions were observed separately. The accepted first-token
SRAM baseline was restored and checked after the first run. This is a real
board result for the Task 5 family image,
but the route and bitstream were made in a temporary diagnostic directory.

The source-owned Nix bitstream at
`/nix/store/48lzs42icd2xmzmyrq971vy2qfiq182a-task5-tinystories-1m-16token-ypcb-bitstream/model.bit`
completed conversion and decoded-mode verification (SHA-256
`78901b3785fd5d33b2d31c9c352c886ca5c510f44505f5479cc9dfe4f1a47553`).
Its `.bit` file differs from the diagnostic file only in the header's source
path and generation time: the configuration payloads compare byte-for-byte
(SHA-256 `f88798f58623410bbfd56defec265c9bcfad8dc3c1826e16deae54966bd9c30c`),
as do the frame files and decoded bit files. The variable header means
byte-for-byte reproducibility of the whole `.bit` file is **not** claimed;
the SV, image, routed FASM and configuration payload have separate identity
evidence. After a fresh, successful SRAM
program of this exact Nix `.bit`, YPCB returned the same frozen sixteen tokens
after one START in 61.564 seconds. The
[raw Nix-bitstream board receipt](../../artifacts/task5/board-nix-bitstream-2026-10-02.json)
is preserved byte-for-byte (SHA-256
`eb1663f4d5d1f4521d8dcb81ad64488b4e03edd35b05a19a5dc21aa707c2a260`).
Its checker does not itself attest to SRAM reconfiguration, so its
`independent_reconfiguration_verified` field remains `false`; the successful
programmer DONE report was observed separately. This is one direct Nix-bitstream
board run, in addition to the three earlier independent diagnostic runs with
the identical configuration payload.

Build-iteration caveat: `task5CompilerSource` in `flake.nix` filters the
repository rather than importing it wholesale, so edits to this report are
excluded. It nevertheless includes almost every Python file under
`scripts/pipeline` and `TinyStories`; a whitespace-only edit to an unrelated
included file changes the source input of all five compiler derivations.
Narrowing that closure requires an explicit dependency audit and a fresh
identity check; it was not part of this acceptance effort.

The committed Task 4 archive verifier was rerun from the Nix development
shell against its raw board transactions, frozen 16-token oracle and
source-rebuilt configuration receipt. It passed the exact continuation
`[11, 612, 373, 257, 1310, 2576, 3706, 20037, 13, 1375, 6151, 284, 711,
2354, 287, 262]` for all three archived SRAM programs (66.40–66.41 seconds
each). This preserves the historical baseline; it is not a new hardware run or
a board test of the Task 5 family-generated core.
