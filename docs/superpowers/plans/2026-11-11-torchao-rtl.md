# TorchAO RTL comparison implementation plan

**Goal:** Add the model and quantization scheme from `~/TorchLean/compile_tinystories.py` as an independent compiler input, preserving the existing input and RTL.

**Architecture:** Reuse the pinned TinyStories-1M snapshot, batch 1 / sequence 32 input and existing lowering stages. Add a TorchAO 0.15 export adapter using `Int8DynamicActivationInt8WeightConfig(version=2)`, per-channel INT8 weights and symmetric dynamic per-token INT8 activations. Keep embedding, normalization and attention floating point. Expand TorchAO operators and signed INT8 matrix multiplication to portable ATen with INT32 accumulation before the existing importer. TorchLean's verification wrapper is not part of this compiler input.

**Constraints:** Existing `direct-*` packages, default package, checks, and result links must retain their outputs. Do not alter either original quantization script. Pin the additional TorchAO dependency without changing compiler versions. Use separate `torchao-*` package names and `result-torchao-rtl` / `result-rtl-comparison` links.

## Tasks

- [x] Add behavioral tests for signed integer accumulation, version-2 quantization scope, exact export/reload and full-model output preservation. Run them before implementing the adapter.
- [x] Implement `model/quantize_torchao.py`, reusing the existing model loader and token windows. Test actual TorchAO eager output against saved, reloaded and importer-frozen exports.
- [x] Parameterize the direct pipeline's input recipe and additional Python packages with defaults that preserve current derivation paths. Add a pinned TorchAO wheel, its export recipe, check and flake packages.
- [x] Build both RTL branches and a comparison derivation containing both RTL trees, a unified diff and size/hash summary. Record original and updated direct derivation paths to verify preservation.
- [x] Run relevant existing checks and the new check, review the diff and document commands and observed results. All flake checks and the candidate's Yosys syntax/module-hierarchy check passed; review findings were addressed.

## Review focus

Check integer sign extension and INT32 accumulation, per-token scale differences and zero inputs, serialization of integer parameters, all Linear layers including the vocabulary head, and unchanged baseline derivation paths. RTLs represent different numerical schemes and are not expected to be bitwise equivalent.
