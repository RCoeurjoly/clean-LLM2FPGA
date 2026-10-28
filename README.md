# clean-LLM2FPGA

Independent reproduction repository for the full TinyStories-1M, on-chip-memory
image generated and loaded on 28 October 2026. Target: YPCB-00338-1P1,
XC7K480Tffg1156-1. Intended core clock: 2.5 MHz.

Exact reference bitstream SHA-256:

```
13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00
```

## Rebuild the identical bitstream

On x86_64 Linux with Python 3.11+, Nix and bubblewrap:

```sh
# Needed only if the pinned store paths are absent:
gzip -dc toolchain/closure.nar.gz | nix-store --import
python3 build.py verify
python3 build.py bitstream
cmp reference/expected.bit build/bitstream/model.bit
```

The `bitstream` command **runs fasm2frames, xc7frames2bit and bitread**, compares
all decoded configuration words, checks SHA-256 and compares the complete output
with the reference. It does not copy the reference bitstream as its output.
The converter embeds wall-clock metadata. `tools/bit_header.py` parses the
Xilinx container and fixes only its date/time fields to the original
`2026/10/28 10:02:51`, retaining the raw converter result as
`model.wallclock.bit`. The configuration payload is unchanged and the normalized
file is independently decoded again. This is container metadata normalization,
not a compiler/model/RTL textual substitution.
It starts from the preserved routed FASM. Use `--output build/another-run` to
repeat; existing output directories are deliberately refused.

The conversion runs in a filesystem namespace with no access to the original
workspace, no home directory and no network. The repository and Nix tools are
read-only; only the new output directory and temporary files are writable.

## Compiler pipeline and upstream replay

`snapshot/deliverables/tasks-4-5-6/` contains the compiler pipeline-v2 sources,
native compiler passes, RTL, model/reference data, Nix recipes and flake locks.
`snapshot/artifacts/task6/board-fit-2026-10-25/` contains the selected build's
recorded dependency closure, native pass helpers, stage recipes, checkpoints,
and historical evidence required by its admission checks. This is a conservative
reproduction snapshot, not a claim that every historical experiment is needed
by a future minimal compiler. The existing license commit is preserved; upstream
license and attribution files remain alongside the copied source.

The actual approach is typed compiler passes through MLIR/Calyx, on-chip memory
and fixed-point resource reductions, native Yosys mapping, MUX lowering,
nextpnr packing/placement/routing, then Project X-Ray bitstream assembly.
No model or RTL textual substitutions were added for this extraction.

Inspect and replay individual recorded stages:

```sh
python3 build.py list-stages
python3 tools/replay.py \
  artifacts/task6/board-fit-2026-10-25/probe/full-model-shared-range8-v1/calyx-ir \
  --output build/replay-calyx-ir
```

Replay verifies recorded input/tool hashes, runs the exact recorded command,
and compares output hashes. It mounts the copied snapshot at the historical
absolute path **inside a private namespace**. The real old checkout is neither
mounted nor needed. Source files and embedded IR locations are not rewritten.
The original host-tool dependency hashes are in `provenance/host-dependencies.json`;
upstream stages using `/usr` tools additionally require matching host tools.

To rerun the full native router from the exact preserved placed checkpoint:

```sh
python3 tools/replay.py native-route --check-only --output build/route
python3 tools/replay.py native-route --output build/route
```

This uses the original native command, seed 15, router settings, physical input,
XDC and clock hook; allows 12 hours; retains the resulting routed JSON even on
failure; and compares FASM against the original SHA-256. The original run took
about 10 hours 40 minutes. A new full routing run has **not** been completed as
part of extraction. End-to-end source-to-bitstream determinism has therefore
not been newly established. The short `bitstream` command is FASM-to-bitstream
reproduction, not a fresh model-to-bitstream compilation.

## Preserved hardware limitation

This is an exact reproduction of a **failing hardware experiment**, not a
working-inference release. The original full route reached zero routing
conflicts and passed reported core timing. A post-route clock-divider topology
check failed. The user authorized a hardware experiment with that check
unresolved. Bitstream conversion and frame decoding passed; SRAM programming
reported INIT/DONE. The JTAG reset response was `0e00000000000000` instead of
zero at both 1 MHz and 100 kHz. Inference never started; zero tokens were
compared. The source CSR simulation did not reproduce this hardware failure.

Original logs and test results are under
`snapshot/artifacts/task6/board-fit-2026-10-25/probe/full-model-authorized-hardware-trial-v1/`.
Rebuilding does not program hardware or alter flash.

## Preservation and size

`reference/expected.bit` is the exact loaded image; `reference/route.fasm` is
its source configuration. `provenance/files.json` records SHA-256 for every
copied file. The compressed Nix archive preserves the executable toolchain,
libraries and store-resident model/compiler inputs independently of garbage
collection. It is intentionally included in Git for this local archival repo.
This snapshot is several GB; it is not prepared for hosting services with
small per-file limits. No remote publication is performed.

## Verification performed during extraction

- All 14,271 copied files matched their recorded SHA-256 values.
- Full FASM-to-frames conversion produced identical frames and configuration
  payload with the old workspace hidden. After timestamp normalization, `cmp`
  and SHA-256 confirmed the entire bitstream was identical.
- A full-model SCF-to-Calyx stage replay produced the exact recorded bytecode.
- Three header-parser tests passed, including malformed data and unchanged payload.
- The archived 359-path Nix closure imported successfully.

Receipts are in `provenance/bitstream-rebuild.json`,
`provenance/calyx-stage-replay.json`, `provenance/isolation-check.json`, and
`provenance/toolchain-archive.json`. No new full synthesis/routing or hardware
programming was run during extraction.
