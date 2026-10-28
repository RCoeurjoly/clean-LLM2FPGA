# Exact-input TinyStories-1M compiler design

**Status:** accepted

## Purpose

Determine what the existing LLM2FPGA compiler pipeline can actually lower when
its input is the exact TinyStories-1M quantized execution used by the proven
kev-gpt-derived accelerator. The observed compiler frontier, rather than an
assumed backend architecture, will determine the next implementation step.

The immediate objective is diagnostic and causal: remove model identity,
package layout, quantization, tokenizer, and arithmetic semantics as possible
explanations before debugging Torch-MLIR, CIRCT, Calyx, generated RTL, resource
use, or board integration.

## Scope

In scope:

- the exact TinyStories-1M model and tokenizer used by the proven accelerator;
- the complete authenticated kev-gpt package, including weights, scales,
  calibration inputs, layout, and hashes;
- an executable PyTorch representation of the same quantized/fixed-point
  computation;
- the existing PyTorch -> Torch-MLIR -> CIRCT/Calyx -> SystemVerilog pipeline;
- capture and classification of the first reproducible compiler frontier;
- evidence-driven selection of the next compiler or architecture change;
- eventual BRAM-only TinyStories-1M inference on the YPCB.

Out of scope until the exact-input frontier is established:

- arbitrary PyTorch models;
- other TinyStories sizes;
- the Representative Core as an active milestone;
- DDR3 or PCIe integration;
- speculative custom RTL kernels, new scheduling architectures, or backend
  substitutions;
- resource comparisons between semantically different models.

## Frozen deployed YPCB model contract

The compiler target is the exact TinyStories-1M configuration already shown
performing BRAM-only inference on the YPCB. It is not a newly selected or
approximately equivalent TinyStories model:

| Field | Frozen value |
|---|---|
| Hugging Face model | `roneneldan/TinyStories-1M` |
| Immutable model revision | `ac533fb8b4f69c71894bf96badfe11e6294d9fcf` |
| Architecture | GPT-Neo; 8 layers; hidden size 64; 16 heads; head dimension 4 |
| Vocabulary and context | 50,257 tokens; 32-token maximum runtime context |
| Other model semantics | tied word embeddings; `gelu_new`; greedy top-1 generation |
| Weight representation | signed INT8, symmetric, one scale per output channel (`symmetric_int8_per_output`) |
| Activation representation | signed INT8 with 97 named per-channel Q/DQ scale vectors of width 64 or 256 |
| Bias and LayerNorm package representation | FP32, converted to signed Q16.16 for the deployed fixed-point execution |
| Internal value format | signed Q16.16 |
| Hardware scale format | unsigned Q8.24 |
| GEMV accumulation | signed 64-bit serial accumulator |
| Nonlinear and normalization behavior | deployed integer/fixed-point LayerNorm plus LUT-based GELU and attention softmax |

The canonical package is
`/home/roland/kev-gpt/.worktrees/kintex-selftest/model_packages/tinystories-1m`
and is frozen by these SHA-256 values:

- `manifest.json`: `374171e8c0a06dc2632434965f218cf2fc6c82ee15470c47a958b6b9f5f6ca35`;
- `weights.bin`: `caa140a70f824334d626e20819effabb3a56c28f35cc5c84e6f5f174b3f6bf4e`;
- `scales.bin`: `a81faadf9ab21a525a8a20870f2fa97572c66bbf6b88c5cbe2a29cd253355155`;
- `calibration_ids.bin`: `2537125a6edea656c5f6b8fe537b4cec7f2a3b2f633f5ee36297135e705bb075`;
- tokenizer JSON: `f6ed3d307010c244c22aeffbde05f419cf277c23e64cf98b673cac5449cfeff5`.

The frozen generation fixture is prompt `Once upon a time`, prompt token IDs
`[7454, 2402, 257, 640]`, and the following 16 greedy output token IDs:

```text
[11, 612, 373, 257, 1310, 2576, 3706, 20037,
 13, 1375, 6151, 284, 711, 2354, 287, 262]
```

This table and these hashes are the compiler input contract. Gate 0 may refine
the bit-exact ordering, rounding, saturation, overflow, and nonlinear details
from the deployed sources, but it may not silently substitute a different
checkpoint, quantization policy, package, tokenizer, or generation fixture.

## Gate 0: exact executable identity

“Same model” means the same executable computation, not merely the same model
name or Hugging Face checkpoint. Gate 0 freezes and verifies:

- Hugging Face model ID and immutable revision;
- model configuration and architecture dimensions;
- kev-gpt source revision and any relevant working-tree patch;
- package manifest and every package-file hash;
- tensor names, shapes, offsets, layouts, and tied-weight rules;
- weight and activation quantization granularity;
- scale representation and application order;
- accumulator widths, rounding, saturation, overflow, and nonlinear semantics;
- tokenizer files and configuration;
- prompt token IDs, greedy-selection rule, expected 16 tokens, and named
  intermediate checkpoints.

The historical reference contract is not accepted unchanged. Its per-tensor
activation and signed-INT32 accumulator descriptions contradict the deployed
package and implementation summarized above. Gate 0 must preserve the deployed
per-channel activation vectors and signed 64-bit GEMV accumulation, and resolve
remaining lower-level semantic conflicts by tracing the package generator and
executable reference. Missing authority produces an explicit blocked result.

## Exact PyTorch input

The compiler input is an authenticated PyTorch program that executes the frozen
package semantics. Reconstructing floating-point weights from the package is
insufficient: the program must include the observable quantization,
dequantization, fixed-point conversion, rounding, saturation, accumulation,
LayerNorm, Softmax, GELU, residual, and token-selection behavior used by the
accelerator.

Before compiler lowering, the exported program must match the executable
kev-gpt reference for:

1. package and model identity;
2. the frozen prompt and 16 generated tokens;
3. named block-0 token-step checkpoints;
4. logits or their authenticated fixed-point equivalent at each selected
   token; and
5. deterministic repeated execution.

Failure here is an input-model problem, not a compiler problem.

## Current-pipeline experiment

Once Gate 0 passes, feed the authenticated exported program into the existing
pipeline:

```text
authenticated kev-gpt package + frozen model structure
        -> exact quantized PyTorch program
        -> torch.export
        -> Torch-MLIR
        -> existing Linalg/SCF normalization
        -> existing CIRCT/Calyx lowering
        -> existing SystemVerilog route
```

Only the authenticated input adapter and provenance wiring may change before
this experiment. Do not add external RTL primitives, replace nonlinear
operators, change quantization, or introduce a new scheduler merely to move the
frontier.

Every stage records its input and output hashes, tool revisions, command line,
operation census, diagnostics, and validity. Partial output after an upstream
diagnostic is not a successful lowering.

## Frontier classification

The first failure is classified at the earliest causal boundary:

- `identity_frontier`: the package or executable semantics remain ambiguous;
- `export_frontier`: the exact PyTorch computation cannot be exported;
- `torch_mlir_frontier`: Torch-MLIR cannot represent or lower an operation;
- `pre_calyx_frontier`: required normalization leaves unsupported IR;
- `calyx_frontier`: CIRCT/Calyx rejects or mis-lowers the valid input;
- `sv_frontier`: Calyx cannot emit valid synthesizable SystemVerilog;
- `functional_frontier`: generated RTL executes but differs from the oracle;
- `resource_frontier`: functionally correct RTL cannot fit;
- `timing_frontier`: fitting RTL does not meet constraints;
- `board_frontier`: timing-closed RTL fails deterministic board inference.

The frontier receipt must contain a minimal reproducer when practical and the
first mismatching checkpoint or diagnostic. Later cascading failures are not
reported as independent blockers.

## Post-frontier decision gate

No replacement architecture is selected in advance. The observed frontier
determines the smallest justified response:

- repair or add a compiler pass when the representation is sound but lowering
  is missing;
- define a bit-accurate compiler primitive when a supported TinyStories
  operation has no viable generic lowering;
- introduce scheduling, memory, streaming, or reuse abstractions when correct
  generated RTL is structurally too large or slow;
- introduce a stable autoregressive runtime boundary only when the tensor
  compiler output is correct but cannot express the complete token lifecycle.

StreamTensor is relevant precedent for the latter two cases: it treats
individual kernels as designed/generated components and compiles their tiling,
fusion, streaming, buffering, DMA, and runtime composition. It is not evidence
that arbitrary tensor semantics can be lowered mechanically into efficient RTL.

Any chosen change must be tied to the exact frontier, preserve the authenticated
input contract, and pass a before/after regression at that boundary.

## Approved bounded serial-GEMV salvage route

The authenticated full-model experiment established a `calyx_scalability_frontier`:
the generic SCF-to-Calyx route produced neither a diagnostic nor a candidate after
24 hours. This does not prove that CIRCT cannot lower the model. It does prove that
the exported scalarized form is not an acceptable development route. The next
experiment is therefore a bounded compiler architecture change, not another run of
the same route.

The compiler may introduce one explicit `llm2fpga.serial_gemv` boundary at every
call currently implemented by `serial_gemv_accumulate`. Its semantic contract is
fixed by the executable oracle:

- W8 per-output-channel weight codes and the existing activation Q/DQ boundaries;
- one output accumulator per output row, with input indices visited in ascending
  order;
- signed 64-bit two's-complement wrapping multiply-accumulate;
- existing Q16.16 values, Q8.24 scales, rounding, bias, and output Q/DQ behavior;
- exact boundary values and hashes for the frozen prompt and all 16 generated
  tokens.

The eager implementation remains the oracle. `torch.export` must retain this as a
compiler-owned custom operation, and the raw Torch-MLIR import must retain the
operation as an explicit boundary. A pass plugin built against the *pinned
Torch-MLIR* ABI, before the fixed Torch backend legalization, is the only permitted
legalization point. It must convert only this operation and its statically proven
shapes into the compiler's defined serial-GEMV representation. It must reject every
other custom operator or dynamic shape.

The downstream compiler must lower that representation into a defined Calyx
component and `calyx.invoke` site. The component is generated from this declared
contract and compiler source; it may not import, wrap, instantiate, or copy
kev-gpt RTL. Its storage/interface realization is an implementation decision that
must be evidenced by the first bounded stage artifact.

This route deliberately does not promise arbitrary PyTorch support, vectorized
GEMV, DDR3, PCIe, a Representative Core substitution, or board inference before
the compiler stages are valid. It is a test of whether the compiler architecture
can preserve exact TinyStories semantics while expressing the serial structure that
the generic scalarized route erased.

### Fast-learning gates

Every development-stage command has a 30-minute wall-clock limit and must produce
either a nonempty hash-bound artifact or a deterministic useful diagnostic. A
full exact-model build has a 2-hour wall-clock limit. Timeout receipts bind input,
tool, command, output paths, process identities, termination, and materialized
stage state; no later stage may run after a failed gate. An unbounded compiler run
is prohibited.

The semantic gates are ordered:

1. eager oracle versus exported custom-boundary hashes for the frozen token step;
2. raw Torch-MLIR boundary presence and legalizer output contract;
3. Linalg/SCF preservation of the compiler-owned call;
4. defined Calyx component/invoke and serial MAC trace equivalence;
5. generated SystemVerilog syntax, synthesis, and provenance closure;
6. full frozen 16-token equivalence only after stages 1--5 pass.

Failure at any gate is new evidence for the salvage/cut-loss decision. It is not
permission to weaken quantization, arithmetic, ordering, or provenance.

## Validation ladder

1. **Identity gate:** exact source, package, tokenizer, and semantics are
   authenticated and internally consistent.
2. **Executable-input gate:** PyTorch/exported execution matches the kev-gpt
   oracle and frozen 16-token result.
3. **Stage-validity gate:** each current-pipeline stage either produces valid,
   hash-bound output or a precise frontier receipt.
4. **RTL functional gate:** if RTL is produced, simulation matches the earliest
   available oracle checkpoints before resource work begins.
5. **Implementation gate:** Yosys and constrained nextpnr report fit and timing
   without weakened constraints.
6. **Hardware gate:** BRAM-only YPCB inference matches the frozen 16 tokens on
   three cold starts.

Gates 4–6 are conditional on resolving the first observed frontier; they are
not assumed consequences of completing Gate 0.

## Required artifacts

- corrected exact-input contract and conflict-resolution report;
- complete package/source/tokenizer provenance manifest;
- executable quantized PyTorch adapter and exported-program hash;
- kev-gpt-versus-PyTorch checkpoint and token-equivalence receipt;
- per-stage current-pipeline manifests;
- first-frontier receipt and minimal reproducer;
- decision record for the evidence-backed next change;
- when reached, RTL simulation, synthesis, timing, and board evidence.

## Provenance and compliance

The exact package and kev-gpt executable are reference evidence. Their source
is not silently copied into compiler-generated deliverables. Any reference-
derived semantics or implementation choice is documented, and LLM assistance
remains disclosed. NLnet guidance is required before reference-derived work is
treated as a funded deliverable.
