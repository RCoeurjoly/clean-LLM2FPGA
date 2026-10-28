# Compilation input boundary

`package_inputs.py` authenticates the snapshot configuration/checkpoint and
package manifest/weights/scales. It reads no receipt, quality report, expected
result or historical adapter. Revalidation checks retained weight/scale bytes
against their recorded package hashes as well as the materialized-state seal;
recomputing that seal cannot silently replace these payloads.
The caller selects the reviewed numerical contract;
a changed contract has a changed recorded identity, not implicit acceptance.

`package_model.py` materializes package tensors and constructs graph-visible
integer PyTorch computation. Token IDs remain runtime arguments. The checkpoint
is authenticated for provenance, without loading Python objects from it.
Currently only the checked 1M geometry is supported: broader frontend support
remains an open obligation. The downstream compiler cannot dispatch by model name.

Rules retained from the frozen exact adapter:

| Rule | Implementation |
| --- | --- |
| Package parameter Q16.16 and scale Q8.24 | `materialize_tensors`: float64 multiplication, ties-to-even rounding, range checks |
| Runtime right-shift rounding | `round_shift_signed`: nearest, ties away from zero; avoids absolute value of INT64_MIN |
| Activation conversion | `activation_qdq`: per-channel scales and signed INT8 saturation |
| Matrix product | `serial_gemv`: ascending-index signed i64 wrapping MAC |
| LayerNorm | `_fixed_layer_norm`: truncating mean, floor variance, restoring integer square root, Q32 epsilon 42950 |
| GELU | Mathematical tanh approximation LUT, Q12 interpolation |
| Attention | Causal prefix, score shift 24, exponential LUT, signed rounded weighted mean |
| Output head | Tied embedding weights/scales, i64 MAC and rounding |

The profile is `tinystories-1m-integer-v1`. Numerical changes require a new
profile and renewed CPU/IR/RTL evidence. LUTs encode mathematical functions,
rather than answers to a test prompt. The model has no observation hooks.

`dependencies.json` lists compile-only sources. The separate verification
command `verify_graph_model.py` checks all 257 materialized buffers against the
frozen adapter and all 16 last-row logit vectors against pinned independent NumPy.
`verify_graph_input_isolation.py` copies only declared sources and authenticated
inputs into a fresh directory, ignores inherited PYTHONPATH, and executes two
different same-shape token inputs. It copies no fixtures or historical adapters.
This checks the frontend boundary; production Nix and RTL checks remain pending.

Evidence: `artifacts/pipeline-review/graph-model-reference.json` and
`graph-model-isolation.json`. Neither is a compiler dependency. Frozen reference
files remain unchanged. Arithmetic functions were copied once by AST extraction
for this source refactor; compilation performs no source or IR text rewriting.

# Capture boundary

`capture.py` records graph code/signature, parameter and constant tensor hashes,
runtime input shapes/dtypes, operator inventory and node source locations.
Example tensor values do not enter the canonical program identity. Strict export
retains tensor arguments; tests replay changed values and changed buffers.
`operator_support.json` distinguishes capture support from pending lowering:
recognizing an operator at capture does not establish hardware support.
`import_raw_torch` invokes the raw upstream importer and verifies the module;
backend legalization follows later through native passes. It performs no IR text
rewriting. `verify_graph_capture.py` records four-token 1M capture/replay/raw-import
evidence without loading expected answers. Large graph/source/MLIR artifacts are
retained in the execution directory, not copied into the review receipt.
