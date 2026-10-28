# Native Torch passes

The driver links the pinned shared MLIR library and Torch aggregate C API library.
Torch's installed C++ type implementations have hidden visibility. Using its typed
C API keeps parsing and shape/dtype queries within the same Torch dialect library;
it avoids duplicate TypeIDs and printed-type parsing. `TypedTensor.h` checks static
signed i64 types and converts them to standard signless i64 tensor types.

`llm2fpga-lower-serial-gemv` replaces the raw custom operation with bridge operations,
a zero-filled tensor, and a Linalg reduction over row/output/input indices. Multiply
and add have no overflow flags. Loop lowering retains ascending input order. Unknown
custom operators and inconsistent types/shapes fail at their original IR location.

`llm2fpga-lower-exact-integer-ops` uses arithmetic signed shifts and masks, wrapping negation, signed
floor/truncating integer division and positive-divisor remainder. Shifts require a constant in [0,63); broadcast
division checks static shapes. Division is defined for nonzero divisors and excludes
signed overflow INT64_MIN/-1, as required by the declared numerical workload. These
operator domains must be retained when extending the frontend's accepted models.

Raw import requires the `torchdynamo-export-to-torch-backend-pipeline` normalization
sequence before `torch-backend-to-linalg-on-tensors-backend-pipeline`. Calling the
function frontend pipeline is not equivalent. Interchange acceptance requires a
verified module with no Torch or compiler-specific descriptors. The numeric
boundary fixtures execute the lowered code through the matching LLVM23 runner.

The pinned Nix SDK and installed driver now build and execute the typed GEMV
install check. CMake preserves the external SDK library paths at installation;
the test does not rely on LD_LIBRARY_PATH. Full-model capture/lowering receipts
are being refreshed against this SDK. Downstream hardware acceptance remains open.

The pinned importer leaves model buffers as explicit function arguments. Their
identities remain in the capture receipt. The native Calyx memory-binding passes
authenticate initializer hashes, geometry, mutability and caller references before
SV emission; see `calyx/README.md`. Initialized GEMV RTL comparisons and a Yosys
memory-content check verify this component boundary. The TinyStories block's
initialized RTL is generated, but its complete runtime comparison remains open;
these component checks do not establish full-model or board acceptance.
The rounding fixtures use immutable PyTorch parameters so upstream import freezes
those fixture values. Their expected answers stay in the verification command.

`llm2fpga-test-scalar-entry` is verification-only. It extracts the computed rank-zero
tensor and changes the no-argument test entry's return ABI to i64 for the LLVM
runner. It supplies no computed value and is excluded from the production pass
sequence. It rejects other shapes/signatures.

Before allocating buffers, the shared profile runs the pinned upstream
`linalg-fuse-elementwise-ops` pass on typed tensor IR. It combines compatible
producer/consumer elementwise bodies, removing intermediate tensor traversals.
It does not substitute a model-specific formula or supply computed answers.
The pointwise-chain regression checks that three separate traversals become one.
The shared LLVM checks exercise 64 rounding/quantization cases and three signed
sliced-memory cases; twelve runtime RTL cases cover matmul, initialized GEMV and
composed Q/DQ plus GEMV. These are fixture domains, not whole-model equivalence.

For the actual four-token transformer block, this pass reduces Linalg generic
operations from 2,579 to 600 and generated RTL from 51.1 MB to 19.3 MB. These are
structural measurements. Full block outputs and FPGA fit remain unverified.

The shared profile uses conservative copy-before-write bufferization: default
alias analysis exceeded 600 seconds on the full graph; the conservative mode
finished the same boundary in 1.5 seconds. Copies are explicit IR, lowered by
the upstream typed `refback-munge-memref-copy` pass before Linalg loop conversion.
This pass copies tensor elements and has no access to reference answers. Alias
folding moves slice offsets into load/store indices; `lower-affine` then expands
the resulting index expressions. `arith-expand` legalizes signed floor division
before CIRCT. Conservative allocations still require Task 6 lifetime/sharing
optimization; this change does not establish FPGA fit.

`llm2fpga-lower-integer-math` runs after scalar loop lowering. It replaces
integer absolute value with signed comparison, wrapping subtraction from zero,
and selection. In particular, `abs(INT64_MIN)` remains `INT64_MIN`, as in
PyTorch. Unlowered vector absolute values fail explicitly. The upstream
`math-expand-ops` pass leaves this operation intact and cannot replace this
boundary. Native LLVM execution tests cover zero, signed values and extrema.

Raw serialization and every optimizer boundary retain PyTorch source locations
through native MLIR printing. `verify_graph_memory_lowering.py` checks the exact
shared buffer/loop pass sequence on sliced tensors with signed floor division,
against both PyTorch and an independent integer calculation. Its receipt states
the verification-only scalar ABI scope; runtime RTL checks remain separate.

Executable RED/GREEN findings: upstream floor/trunc division returned wrong signed
results and rounded INT64_MAX; remainder(-3,256) returned -3; neg(INT64_MAX) lost one
unit. Explicit integer division, sign-corrected remainder and wrapping negation
repair those cases. Run the integer pass again after TorchDynamo normalization to
catch arithmetic introduced by upstream decomposition. The native standard-integer
boundary rejects a remaining Torch/custom dialect or floating-point type.

Latest local checks: 64 composed PyTorch rounding/quantization cases match eager
execution and compiled LLVM execution; signed shifts/division/remainder/negation,
saturation boundaries and wrapping MAC fixtures pass. An earlier combined checkpoint ran 79
tests with 11 preserved historical optional skips. The four-token full model
reaches the verified standard integer boundary. Receipts are separate verification
artifacts (`graph-rounding-native.json`, `graph-integer-lowering.json`).
# Scratch storage reuse

`llm2fpga-reuse-buffers` runs as the explicit `scratch` stage after bufferization
and before converting Linalg to loops. It shares only private static allocations
with equal types and alignment. Function arguments, aliases, opaque uses, frees
and extra allocation annotations are excluded. Nested uses occupy their entire
enclosing operation, so conditional or loop uses conservatively overlap.

The first use must prove a complete write: a copy from a different buffer, a
fill, or a Linalg generic whose old output argument is unused and whose static
index maps cover every output element. Lifetimes are collected before rewriting;
the previous buffer's last use must precede the next buffer's first use strictly.
This excludes copy source/destination aliasing and reuse of uninitialized or
partially initialized storage. The actual four-token block IR reduces scratch
allocations from 2,668 to 279. That count is structural evidence, not a complete
block correctness or FPGA-fit claim.

`llm2fpga-strength-reduce-integer-constants` runs on scalar loop arithmetic.
For signless integer signed division by a positive constant power of two, it
adds `divisor - 1` only to negative numerators, then applies an arithmetic right
shift. This preserves truncation toward zero. Remainder is reconstructed as
`numerator - (quotient << shift)`. The pass preserves operand order and source
locations and adds no overflow flags. Index operations retain their type and
use the same rewrite only for divisors at most `2^30`, positive in both the
production 32-bit Calyx and 64-bit CPU index domains. Larger index constants,
vectors and other divisors remain unchanged. Native execution checks cover both
index widths, signed extrema and negative nonmultiples; initialized and composed
RTL regression checks remain separate.


`llm2fpga-fold-producer-slices` runs after elementwise fusion and before
bufferization. It asks MLIR's native tiling interface to compute a sole
consumer's static, rank-preserving, unit-stride slice of a pure single-result
Linalg generic or fill. The interface derives input slices from index maps;
reduction dimensions remain complete. Multiple consumers, dynamic bounds,
strided slices and unsupported result maps remain unchanged. A strict shape
reduction prevents repeatedly tiling full-size initializer slices. Native tensor
empty folding shrinks single-use initializers. No expected outputs enter this
pass. The driver registers upstream MLIR extensions, including the interfaces
needed by standalone tensor fixtures.

The six native tests include all six selected-row MAC outputs against PyTorch
and an independent wrapping integer reference. The actual context-4 generation
step passes the shared buffer stage and its final projection has one row.
These checks do not prove complete TinyStories RTL execution. Run the reproducible
check with `nix build ./deliverables/tasks-4-5-6#graph-producer-slices-check --no-link`.


`llm2fpga-fuse-pointwise` uses upstream Linalg fusion patterns with a single-use
producer, an all-parallel consumer, and a permutation input map. These conditions
exclude cloning a shared producer and repeating preprocessing across reduction
or broadcast dimensions. The candidate is qualified separately before selecting
it in the shared profile. Five native tests include captured PyTorch outputs
checked through LLVM against eager execution and an independent integer reference.
Run `nix build ./deliverables/tasks-4-5-6#graph-pointwise-fusion-check --no-link`.

On the actual context-four block, unrestricted fusion raised loop-weighted
signed divisions from 5,376 to 264,960. The constrained candidate restores 5,376,
reduces weighted loads from 858,620 to 638,676 and stores from 371,720 to 219,055,
and retains 226,928 multiplications compared with 227,952 before fusion. These
are typed structural counts with branch regions summed, not cycle bounds or
complete numerical RTL evidence.


The shared buffer stage now selects `llm2fpga-fuse-pointwise` before the producer
slice pass and bufferization. Its replay passes nine profile tests, 64 rounding
and quantization cases, three signed-slice cases, six producer-slice tests, five
fusion tests and twelve shared kernel RTL cases. Complete transformer-block
execution and sixteen-step RTL feedback remain separate open gates.


`llm2fpga-link-generation` links sixteen already-bufferized, graph-derived
functions marked with ordered `llm2fpga.context_length` values 4 through 19.
It requires one runtime token buffer per function, consistent authenticated
frozen inputs, and two `bufferize.result` outputs: a rank-zero i64 token and a
fixed-length logits vector. It validates every function before emitting the
controller. Private context storage holds the four runtime prompt tokens and
sixteen computed outputs. Each synchronous call finishes before its token is
read, appended, and copied into the next function's prefix. Frozen inputs and
last-step logits storage are shared; model operations remain in the imported
functions. Private allocations map to persistent hardware memories.

The upstream `buffer-results-to-out-params` option `add-result-attr=true` supplies
the output markers. A separate ABI test qualifies it; the shared single-context
profile has not yet enabled it. Native Calyx conversion of multiple functions
also requires `top-level-function=main`. Run
`nix build ./deliverables/tasks-4-5-6#graph-generation-link-check --no-link`.
The gate checks two runtime feedback sequences, every generated token, final
logits, unchanged runtime/frozen inputs, rejected contracts, and native Calyx
lowering. These are small controller fixtures. Importing and executing all actual
TinyStories context functions through RTL remains an open gate.


`llm2fpga-import-generation` accepts an ordered `files` list for contexts 4–19.
Each file must parse as an MLIR module with a defined `main`; function-only
modules with no module attributes are supported. It renames every function and
its symbol references through `SymbolTable`, using a separate namespace per
context, before cloning the validated modules into an empty destination. The
renamed entry receives its context length and all imported functions become
private. The following linker verifies input and output contracts. This import
adds no model computation and reads no expected-output files. The native gate
also checks that helper names repeated across modules keep their own callees.


The owned orchestration entry `compiler.generation_driver.link_generation_functions`
accepts sixteen `ContextFunction` records pointing to verified buffer stages.
Before invoking either native pass it authenticates each stage specification,
receipt, declared dependencies, tool identities and buffer output. A failed
rerun invalidates the earlier link success receipt. It creates an empty module
as the import destination, declares all 49 direct inputs, and delegates geometry
and feedback control to the native passes. It handles JSON metadata and tool
arguments; it never edits generated computation. The generation-link Nix gate
also checks stale inputs/IR and runs this entry against real native boundaries.


The optional `llm2fpga-fuse-horizontal-pointwise` candidate joins pure tensor
branches with the same static parallel iteration domain and identity output
maps. It requires a shared input and proves that moving the earlier result to
the later branch crosses no dependent use. Inputs with equal SSA values and
maps are deduplicated, allowing the existing pointwise pass to consume a shared
producer once. Each old scalar body is cloned once and both outputs survive.
Search is bounded to 64 nearby operations; unsupported cases remain unchanged.
Four Nix tests include signed-extreme PyTorch/LLVM outputs, shared empty output
initializers, an earlier-use barrier, and preserved reduction/broadcast work.
Actual context-four loop count falls from 26,101 to 24,977. This candidate is not
selected in the shared profile and does not prove model RTL equivalence or fit.


`llm2fpga-share-private-functions` operates after corrected rank-one memory
flattening. It matches ordered nonconstant SSA, regions and all operation
attributes across private functions called exclusively from the root block.
Unannotated scalar integer constants are matched at each operand use. Differing
uses become parameters, deduplicated by type and the complete vector of values
across variants; sharing a constant definition never forces separate uses to
have the same parameter. Calls, aliases and capacity observers remain outside
the proof. Rank-one private capacity may grow only for indexed access and
compatible exclusive callers. Captured-specialization metadata is retained in
`llm2fpga.shared_variants`. See `tests/test_graph_function_sharing.py` and
`artifacts/graph-constant-use-sharing` for the pooled-constant regression,
complete-model CPU and runtime-bounds checks. Complete RTL numerical and fit
acceptance remain open; CPU equivalence is not RTL equivalence.


`llm2fpga-protect-functional-inputs` declares the functional tensor ABI before
standard One-Shot Bufferization analysis: tensor inputs are readonly, and an
existing conflicting writable contract fails. The pinned module-bufferization
implementation honors `bufferization.writable=false`; tests require a separate
result allocation even for a destination tied to an input tensor.

`llm2fpga-compact-fixed-dim-storage` runs after loop lowering, alias folding and
standard integer-range simplification. It visits private static identity-layout
allocations without extra annotations. All uses must be direct loads/stores. A
dimension shrinks only if every access uses the same valid constant coordinate;
that coordinate becomes zero. Other dimensions retain their extents and indices.
Aliases, calls, shape/stride observers and unknown annotations exclude the
allocation. Invalid constant accesses are never repaired by compaction. The
coordinate mapping preserves all observable values; it does not select model
results. See `tests/test_graph_private_storage.py` and `artifacts/graph-private-storage`
for native, full CPU and bounds checks. RTL numerical and fit gates remain open.
