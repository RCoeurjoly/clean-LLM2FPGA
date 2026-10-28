# Fixed-point reference contract

This is the numerical contract of the accepted TinyStories-1M fixture, not
an assertion that INT8 logits equal the original floating-point model.
`model_adapter_exact_package.py` is the executable PyTorch reference.
`family_fixed_point_oracle.py` reuses its arithmetic for validated GPT-Neo family geometry.
The separately pinned NumPy implementation supplies an independent executable comparison.

| Operation | Representation and rule | Executable owner |
|---|---|---|
| Weight package | Per-output signed INT8 codes and little-endian float32 scales | `_tensor_images` |
| Scale materialization | Round float64(scale) × 2^24 to nearest, ties to even; require 0 < code < 2^24 | `_tensor_images` |
| Bias/LayerNorm parameter | Round parameter × 2^16 to nearest, ties to even; signed 32-bit range | `_tensor_images` |
| Q/DQ boundary | Q16.16 input shifted left 8; divide magnitude by Q8.24 scale with half-denominator correction, ties away from zero; clamp code to [-128,127]; dequantize code × scale with signed rounding by 8 | `activation_qdq` |
| GEMV | Ascending input-index signed 64-bit multiply/add with two's-complement wrap; multiply accumulated Q8.24 value by output scale, round-shift by 32, add Q16.16 bias | `serial_gemv_boundary.py`, `_gemv`, `round_shift_signed` |
| Embedding | INT8 code × per-row Q8.24 scale, signed round-shift by 8 | `_embedding` |
| LayerNorm | Mean truncates toward zero; variance uses integer division and epsilon 42950; integer square root; normalized value divides toward zero; gamma affine uses arithmetic right shift by 16 | `_fixed_layer_norm` |
| GELU | Q16.16 → rounded/saturated Q4.12; 8192-entry table over [-8,8), linear interpolation using 3 fractional index bits; return Q16.16 | `_gelu_table`, `_fixed_gelu` |
| Causal attention | Signed dot products shifted by 24; subtract row maximum; clamp delta to [-4096,0]; 4096-entry exp table in Q20, exact zero-delta value 2^20; weighted context division with signed half-denominator rounding | `_attention`, `_exp_table` |
| Residual and logits | Integer residual addition; tied token-embedding output matrix; greedy first maximum selects smallest token index on ties | `_execute`, PyTorch `argmax` |

Calibration determines the activation scales when building a package. Once
packaged, the scales and lookup tables are part of the authenticated reference;
the CPU gate does not recalibrate or silently alter them.

The closure check verifies all 50,257 integer logits for each of the frozen
16 contexts against the pinned NumPy reference, plus greedy-token agreement
with the original PyTorch checkpoint. Floating-point logit differences are
reported without imposing an invented acceptance tolerance.

## Width certificates

`verify_closure_reference.validate_narrowing_bounds` computes per-package
absolute row-sum bounds for all possible INT8 input codes. A 53-bit product
requires a bound below 2^52; a 64-bit product requires a bound below 2^63.
A failed conservative bound is not a counterexample on the frozen prompt.
The historic 1M reachable-domain certificate is narrower in scope; it must
not be silently reused for family models. Selecting optimized widths and
validating the certificate remain compiler responsibilities.

## Reproduction

Run `nix build ./deliverables/tasks-4-5-6#closure-cpu-reference` or the nested
flake check `nix flake check ./deliverables/tasks-4-5-6`. The check uses pinned
package, snapshot and independent reference inputs, and fails if absent.
The final Task 6 image depends on the reference check. The historical export-test file is itself hashed into the frozen predecessor
artifact and is retained byte-for-byte. Its personal-path integration tests
remain optional and may skip; the new required closure CPU check does not.
