# clean-LLM2FPGA

Minimal build inputs for the selected TinyStories-1M PyTorch-to-bitstream flow
on XC7K480Tffg1156-1 (YPCB-00338-1P1, intended core clock 2.5 MHz).

Required final SHA-256:

```
13c0159a9cc94be2c457afdbaa25e65832f095779bf6925df002e2a443599f00
```

The full rebuild is **prepared but not yet verified end to end**. Its execution
was deferred by request. Short checks are recorded in `verification/`.

## Build later

Use x86_64 Linux and Nix. The restored flake uses the selected build's original
locked inputs and tool/model recipes. The exact-image flow also requires the
matching host GCC 13 compiler/headers in `toolchain/host-files.json`.

```sh
# Once, if the recorded tools and model inputs are absent:
nix run .#import-toolchain

nix run .#verify
nix develop
python3 build.py list-stages

# Full reproduction, when ready for the long run:
nix run .#build -- --output "$PWD/build/full"
```

`nix run .#build` launches the complete hash-checked flow in a writable output
directory. It runs outside Nix's build sandbox because the recorded host compiler
and bubblewrap namespace are required. `nix build` installs that launcher; it
does not start routing.

The original source recipes are available as explicit packages, including
`graph-torch-opt`, `graph-calyx-opt`, `graph-calyx-backend`, `graph-yosys`,
`graph-core-1m-stable-scratch`, `nextpnr-xilinx`, `nextpnr-xilinx-chipdb`,
`prjxray`, `prjxray-db`, `model-snapshot` and `model-package`. Their required
install-check sources are retained under `source/toolchain/`. Rebuilding these
packages does not silently replace the executable versions recorded in
`pipeline/flow.json`; exact reproduction uses the authenticated archive.

For short checks without a model or route build:

```sh
nix flake check --no-build
nix build .#checks.x86_64-linux.build-driver
```

The direct Python commands remain available after importing the archive:

```sh
python3 build.py verify
python3 build.py all --output build/full
```

The output directory must be new. Logs and hash receipts stay beneath it,
including on failure. The final image is `build/full/bitstream/model.bit`.
The original routing run took about 10 hours 40 minutes; routing is allowed
12 hours. No command programs hardware.

`all` captures the model in PyTorch, compiles its sixteen context
specializations and controller, then executes the selected 31 native stages:
MLIR/Calyx optimization and memory binding, RTL emission, Yosys mapping,
clock/MUX lowering, packing, placement and routing. It finally assembles and
decodes the bitstream. A missing or changed generated input stops the build;
the final SHA-256 must match the value above.

The build exposes fresh outputs at their original virtual paths so compiler
locations and native commands do not change. The old checkout and cached
compiled core are unavailable inside that namespace. Only pinned runtime
store paths are mounted. Archived compiler intermediates, routed FASM and
reference bitstreams are not build inputs or shipped in this tree.

## Manual review order

1. `source/upstream/compiler/frontend/`: exact historical input loader,
   fixed-point PyTorch model and graph capture source.
2. `source/upstream/compiler/build_core.py`, `generation_driver.py`, and
   `pipeline.py`: context compilation, controller and pass ordering.
3. `pipeline/flow.json`: selected commands, tool identities, generated-input
   dependencies and expected output hashes, in execution order.
4. `snapshot/`: only selected FPGA RTL, XDC, memory configuration and native
   helpers. Long paths are retained because the recorded commands use them.
5. `tools/full_build.py` and `build.py`: orchestration, isolation and acceptance.
   `tools/bit_header.py` fixes only the Xilinx container date/time to
   `2026/10/28 10:02:51`; configuration words are independently decoded and
   compared by `tools/verify_decoded_frames.py`.

`source/toolchain/` retains native compiler sources and patches for review.
The exact-image pipeline runs the pinned executables supplied in the Nix archive;
the flake exposes their source recipes separately. `pipeline/files.json` authenticates the retained
source/configuration/archive files. `pipeline/origin.json` identifies their
origin. Historical experiments, admission drivers, reports and cached compilation outputs
have been removed. The retained tool build tests are inputs to the restored Nix
recipes.

For an isolated assembly check of an already generated route:

```sh
python3 build.py bitstream --fasm /path/to/route.fasm --output build/assembly
python3 -m unittest tools.test_full_build tools.test_bit_header
```

## Hardware status

This reproduces the image from a failing hardware experiment. Routing finished,
but a post-route clock-topology check failed. SRAM programming reported DONE;
JTAG reset failed and inference never started. The direct flow preserves the
actual native routing command and checks image identity; it does not claim
working hardware or turn the failed topology check into a pass.
