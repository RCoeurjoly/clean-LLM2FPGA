# Compiler-generated TinyStories-1M: sixteen-token BRAM board acceptance

On 2026-09-29, the compiler-generated TinyStories-1M candidate matched the
authenticated sixteen-token continuation on YPCB after three independent
FPGA SRAM reconfigurations. This is actual programmed-board inference, not
simulation or memory-trace replay. The accelerator uses on-chip BRAM/LUTRAM,
the unchanged 30 MHz constraints and JTAG transport. No kev-gpt accelerator
RTL was reused.

| Independent SRAM configuration | Exact tokens | Generation and readout |
| --- | --- | ---: |
| run0 | 16/16 | 66.409733 s |
| run1 | 16/16 | 66.409135 s |
| run2 | 16/16 | 66.400391 s |

All runs finished within the 300-second limit. These are independent SRAM
programming operations, not chassis power cycles. The host writes only the
four initial prompt tokens `[7454,2402,257,640]`, sends one START, polls status
and reads indexed output tokens. Generated tokens and intermediate model
states are not supplied by the host. The accelerator performs token feedback.

The frozen continuation is:

```text
11 612 373 257 1310 2576 3706 20037 13 1375 6151 284 711 2354 287 262
```

The two-token `[11,612]` checkpoint passes as a prefix of each full run, not
as a separate host-assisted generation.

## Implementation and evidence

The compiler-generated candidate moves only the immutable projection weight
ROM from LUTRAM to BRAM relative to the previous profile. The compiler control
output is byte-identical to the passing sixteen-token simulation source; the
two memory primitive bodies are behaviorally identical after removing names
and synthesis attributes. All 89 authenticated image files are unchanged.
There was no fresh full-model simulation of this mapping variant, and the
user-cancelled second simulation was not restarted. Actual board acceptance
provides the subsequent end-to-end evidence.

Native synthesis uses 813 RAMB36E1, 89 RAMB18E1, 9,688 RAM64M and 190 DSP48E1.
Routing completed in 28.62 minutes with zero overused wires and zero
architecture failures. The covered accelerator-clock paths report 55.08 MHz,
passing the unchanged 30 MHz target. BRAM/DSP internal timing coverage remains
incomplete: this is **not full timing qualification**.

Qualified bitstream:
`/nix/store/mb1678v9k8cxwwrzsj9brzcms31b6v27-compiler-full16-two-rom-qualified-mode-checked-bitstream/model.bit`.
SHA-256:
`bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f`.
Conversion checks the native FASM memory modes and independently verifies
their decoded physical frame bits, including 29 projection-ROM modes. This
mode check alone does not validate every INIT bit or arithmetic operation.

Hardware archive:
`/nix/store/lgh6ymv5fnsrybqpk8z1apxlm45845j3-compiler-full16-three-sram-board-evidence`.
Derivation:
`/nix/store/hz6whxcrv8x2zimnn8v92a60k80chhzd-compiler-full16-three-sram-board-evidence.drv`.

The durable Nix hardware archive retains the unmodified observed receipt,
raw JTAG exchanges, four SRAM programming logs, executed runner, frozen oracle
and independent verifier. Its check reconstructs all sixteen indexed token
responses from raw TDO exchanges, verifies prompt write/read echoes, one START,
permitted host commands, exact reference tokens and each programming log's
successful configuration flags. No hardware operation is performed by the
archive derivation. The raw inference checker's
`independent_reconfiguration_verified=false` remains untouched; the separate
programming events and logs provide the independence evidence.

After the three runs, the runner restored the accepted first-token SRAM
baseline (SHA-256
`2e28abb6638b407fd55b681018275c712afe47fde2de24ba8d1ebe584208b91a`)
and verified token 11 in 3.204223 seconds. No BPI flash was written and no
privileged board programming was needed.

## Remaining scope

This completes the on-chip frozen-prompt sixteen-token board milestone.
It does not establish general-prompt usability, other model sizes,
PCIe inference, physical DDR3 inference or complete timing coverage.
The active objective continues with the same compiler-generated accelerator
and the validated PCIe/UberDDR3 platform: exact-image simulation, production
calibration/BIST, complete image load/readback, then first-token and
autoregressive hardware gates. The accepted on-chip artifacts must be preserved.
