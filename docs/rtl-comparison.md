# TinyStories RTL comparison

The original `direct-*` packages retain the XNNPACK PT2E input in
`model/quantize.py`. The separate `torchao-*` packages use the model and scheme
from `~/TorchLean/compile_tinystories.py`:

- `roneneldan/TinyStories-1M`, using the existing pinned model snapshot.
- `quantize_(model, Int8DynamicActivationInt8WeightConfig(version=2))`.
- Symmetric dynamic per-token INT8 activations, per-output-channel INT8 weights,
  and INT32 accumulation in Linear layers, including the language-model head.
- Floating-point embeddings, normalization and attention.

Both compiler inputs use batch 1 and sequence length 32, the same model weights,
and the same lowering stages. The TorchLean verification wrapper and tokenizer
are unnecessary for this compiler input. TorchAO 0.15.0 is pinned separately
because the compiler's PyTorch 2.9.1 environment includes TorchAO 0.14.1, which
does not support `version=2`. The external TorchLean environment is not changed.

`model/quantize_torchao.py` unwraps quantized tensor subclasses, stores integer
weights as buffers for PyTorch archive compatibility, expands TorchAO's own
quantization implementations, and expands INT8 matrix multiplication into
signed INT32 ATen matrix multiplication. It preserves TorchAO's rounding, clamp,
scaling order and integer accumulation. The existing importer needs no changes.
The TorchAO branch folds remaining memory aliases after arithmetic preparation
so its final logits reshape can pass the existing Calyx memory flattening stage.

Build the second RTL and the comparison bundle without replacing existing links:

```sh
nix build .#torchao-rtl --out-link result-torchao-rtl
nix build .#rtl-comparison --out-link result-rtl-comparison
```

The bundle contains `baseline/` and `torchao/`, each with `core.sv`, `model.sv`,
`memory.sv` and its own `images/`. It includes unified diffs for the three RTL
files and `summary.json` with file sizes, SHA-256 hashes and memory allocation
counts/bits. Keep each RTL with its matching memory images; `model.sv` embeds
their absolute store paths. Source size and allocated memory are useful
structural comparisons; they do not measure synthesized area or timing.

Intermediate stages are exposed as `torchao-quantized`, `torchao-torch-ir`,
`torchao-linalg`, `torchao-loops`, `torchao-arithmetic`, `torchao-calyx`,
`torchao-futil`, `torchao-core-rtl`, `torchao-rtlil`, `torchao-prepared`,
`torchao-dsp`, `torchao-ram` and `torchao-netlist`.

Run the export checks with:

```sh
nix build .#checks.x86_64-linux.torchao-quantization --no-link
```

The tests check version-2 quantization scope, signed integer accumulation,
dynamic activation ranges including zero and very small inputs, and exact
eager/export/reload/importer-freeze agreement for TinyStories. The two different
quantization schemes are not expected to produce bitwise equivalent logits;
these checks establish export fidelity, not whole-RTL numerical equivalence.

## Generated comparison (2026-11-11)

Both RTL derivations and the comparison bundle built successfully. The original
input, Torch IR, RTL and default/netlist derivation and output paths were checked
before and after the change and remained identical. All flake checks passed,
and the generated TorchAO RTL passed the pinned Yosys SystemVerilog parser and
`hierarchy -check -top model_top` check with all memory images loaded.

| Structural metric | Original XNNPACK | TorchAO v2 |
| --- | ---: | ---: |
| RTL source bytes (all three `.sv` files) | 42,489,524 | 36,978,031 |
| Allocated memory bits | 1,055,456,064 | 878,598,784 |
| Memory instances | 891 | 634 |

The generated TorchAO RTL source is 12.97% smaller and allocates 16.76% fewer
memory bits. The common `memory.sv` implementation is identical. The bundle's
`summary.json` contains the complete file hashes and memory widths. These figures
describe this compiler's generated structure; synthesis area and timing were not
measured.
