# clean-LLM2FPGA

Minimal source build of the selected TinyStories-1M PyTorch-to-bitstream flow
for XC7K480Tffg1156-1 (YPCB-00338-1P1, intended core clock 2.5 MHz).

## Build

On x86_64 Linux with Nix:

```sh
nix build .#bitstream -L
# Equivalent default:
# nix build -L
```

Nix builds the pinned compiler/tool dependencies and model inputs, captures the
model with PyTorch, compiles its sixteen context specializations and controller,
then runs the 31 selected native stages through RTL synthesis, placement,
routing and bitstream assembly. The two Yosys plugins use a Nix-provided C++
compiler. No toolchain import, host GCC, old checkout, or archived build outputs
are required. Each stage is a separate derivation so completed stages can be
reused after a later failure.

The result is `result/model.bit`. A successful build **requires** this SHA-256:

```
13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00
```

Check it independently:

```sh
echo '13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00  result/model.bit' | sha256sum --check
```

The build also checks the original routed FASM hash and independently decodes
the final bitstream to compare its configuration frames. `result/result.json`
contains the acceptance receipt; `result/SHA256SUMS` contains the final hash.
No command programs hardware.

**The new source-built Nix pipeline has not yet been verified end to end.**
That long run was deferred by request. The original routing alone took about
10 hours 40 minutes; this flow allows 12 hours for routing. Building uncached
LLVM/CIRCT and other compiler dependencies adds substantial time and disk use.
The hash checks reject a differing image; the existence of the build recipe
does not prove it will reproduce until the full build succeeds.

## Short checks

```sh
nix flake check --no-build  # Evaluate packages without compiling them
nix flake check            # Build/run the lightweight driver tests
nix build .#mux-plugin .#clock-plugin -L  # Compile the Yosys plugins
```

`nix develop` provides Python and Nix for editing and testing the build driver.
Individual source tool/model packages remain exposed by the flake, including
`graph-torch-opt`, `graph-calyx-opt`, `graph-calyx-backend`, `graph-yosys`,
`graph-core-1m-stable-scratch`, `nextpnr-xilinx`, `nextpnr-xilinx-chipdb`,
`prjxray`, `prjxray-db`, `model-snapshot`, and `model-package`.

## Manual review order

1. `source/upstream/compiler/frontend/`: input loader, fixed-point PyTorch model,
   and graph capture.
2. `source/upstream/compiler/build_core.py`, `generation_driver.py`, and
   `pipeline.py`: context compilation, controller, and compiler pass ordering.
3. `nix/selected-toolchain.nix`, `source/toolchain/nix/`, and `flake.lock`:
   pinned source recipes, compiler patches, and their required build tests.
4. `pipeline/flow.json`: original commands, dependency order, and reference
   output hashes. `snapshot/` contains the static RTL, constraints, memory
   configuration, and native helper scripts those commands need.
5. `nix/bitstream.nix`: maps recorded paths to declared Nix dependencies and
   creates the stage derivations. `build.py` executes a stage and enforces final
   image acceptance.

New Nix store paths and compiler build IDs can change intermediate file hashes.
Each stage records actual hashes and comparisons with the historical hashes in
its `build-result.json`; intermediate matches are diagnostic. Required generated
inputs and outputs must exist. The original FASM and final bitstream hashes are
mandatory. `tools/bit_header.py` fixes only the Xilinx header date/time to
`2026/10/28 10:02:51`; configuration words are independently checked by
`tools/verify_decoded_frames.py`.

`pipeline/files.json` records the retained source/configuration hashes.
`pipeline/origin.json` identifies their origin. Historical experiments,
admission drivers, binary toolchain archives, and cached model/route outputs
are not build inputs.

## Hardware status

The original image came from a failing hardware experiment. Routing finished,
but a post-route clock-topology check failed. SRAM programming reported DONE;
JTAG reset failed and inference never started. Matching the original image
establishes byte identity, not working hardware.
