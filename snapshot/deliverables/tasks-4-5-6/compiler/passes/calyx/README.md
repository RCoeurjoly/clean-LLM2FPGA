# Calyx memory contracts

This driver links against CIRCT's March LLVM/MLIR SDK. It cannot use the
January Torch SDK: the two builds share a major version but have different ABIs.

`llm2fpga-complete-calyx-memory-contracts` handles an omitted `write_data`
assignment only when the same dynamic group has one unconditional `write_en`
driver whose source is a typed hardware zero constant. The write-data input is
then inert, so assigning zero preserves the read transaction. Enabled, guarded,
dynamic or multiply driven enables without data are errors. Validation completes
before any mutation. Existing data assignments remain intact. The pass changes
no clock, control, address, completion or latency signals.

This is a native CIRCT IR pass, before Futil export and Calyx's own verifier.
It does not repair generated source text or initialize model weights. Weight
binding and memory timing verification remain separate compiler boundaries.

Seven executable tests cover successful completion/idempotence, existing data,
rejected write modes and missing pass registration. The installed driver runs
these tests without `LD_LIBRARY_PATH`; CMake retains its SDK/dependency paths.
The pinned `graph-calyx-opt` Nix derivation builds against the pinned
`graph-circt` SDK and runs all seven tests after installation. Both gates passed.
The complete shared profile also passed six changed-runtime-vector RTL cases
with these pinned tools and native assertions enabled: a PyTorch serial GEMV
and the original Task 2 length-16 int32 matmul. Their receipts and retained stage
specifications are in `artifacts/pipeline-review/graph-kernel-*`.

The pinned repaired driver lowers the actual four-token model graph to verified
Calyx IR in 47 seconds. This is a structural boundary check; full-model memory
binding, sixteen-step RTL generation and physical implementation remain open.
The production bitstream still uses the legacy route.

SV emission invokes the pinned Calyx `all` flow by its CLI default. The expanded
pass sequence and source identities are recorded separately in
`artifacts/pipeline-review/graph-calyx-pass-sequence.json`. That flow already runs
`infer-share` and `cell-share`; the latter uses live-range interference and
compatible cell prototypes. Adding a second cell-sharing pass is not a substitute
for checking its applicability, the full workload or FPGA fit. Native memory
binding and the owned backend compatibility patches remain separate boundaries.

The owned SCF conversion preserves the upstream algorithms while deriving block
names from typed regions, collecting control group uses once per pattern and
indexing typed memory input ports once per component. The two control-use caches
have separate lifetimes after control construction; their patterns never change
the control schedule. Memory port indexing applies before primitive binding, when
the pinned conversion creates its registered interfaces as sequential memory cells.
Unknown symbol-table operations retain conservative group-use behavior.

SDK source patches retain missing-group, wrong-kind, condition-driver and unused
group verification. Installed regressions cover 30,000 control groups, 6,400 loops
and 12,000 memories with bounded execution times. All eight upstream SCF fixtures
produce identical lowered IR and the memory fixture produces identical JSON.
The Nix check passes 57 installed native checks and 12 RTL comparisons, including
initialized and composed Q/DQ + GEMV kernels. Evidence is recorded in
`artifacts/pipeline-review/graph-calyx-lowering.json`.

`llm2fpga-validate-memory-bindings` is the first memory-binding boundary. Its
`manifest` and `manifest-sha256` options authenticate a JSON document with
`schema_version: 1` and a `bindings` array. Each entry names a component and
cell and declares width, depth, address width, implementation, read latency,
mutability, and initializer filename/SHA256 (both null for an uninitialized
mutable memory or a reference supplied by its caller).

The pass checks every sequential memory against its declared geometry, reads
and hashes each initializer, and checks the exact hex word count and bit width.
Physical immutable memories require an initializer. Their write enables must
be provably zero. Every component invocation must supply its reference memories
with matching geometry, implementation, latency and mutability. The pass validates
the whole module before attaching `llm2fpga.binding` attributes and the manifest
identity. Twenty-two tests cover validation and replacement alongside the seven
read-contract tests in the installed Nix check.

`llm2fpga-bind-model-memories` consumes that validated IR and an authenticated
compiler-owned primitive source. It creates parameterized hardware declarations
and replaces each sequential memory through typed ports. Caller references keep
their invoke mappings; initializer IDs follow sorted image filenames. Parameter
validation finishes before replacement. No emitted source is edited.

## Memory request retirement

`llm2fpga-retire-memory-requests` runs on typed sequential memories before
`llm2fpga-bind-model-memories` replaces them. For every dynamic group that drives
`content_en`, it collects that group's memory completion signals. Each enable
becomes a sequence: wait until all collected `done` signals are low, then enable
the original group. The wait group drives no memory request. This prevents a
completion from the preceding transaction from completing the new group before
its request occurs. Addresses, data, request guards and the original group body
remain unchanged. The pass handles reference memories in component bodies too.

The owned primitive's completion pulse can remain high after the previous group
finishes. Port annotations alone did not fix the reference-memory reproduction:
a write followed by a read returned zero instead of the written sentinel. The
explicit retirement group returned the sentinel. Binding preserves `go(1)`,
`done(1)` and `interval` metadata; the native Futil serializer retains these
attributes. The retirement pass rejects already-bound memory IR, detects group
name collisions and marks transformed groups so a second run is idempotent.

Read `MemoryRetirement.cpp` with `tests/test_graph_memory_retirement.py`, then
`MemoryBindings.cpp` and the owned primitive in
`scripts/pipeline/memory_policy.py`. Inspect the `memory-binding` stage receipt
for the pass order and the emitted retirement/control groups in Futil. These
operations are IR transformations and native serialization; they do not rewrite
emitted source. This pass establishes a completion boundary; FPGA timing and
full inference acceptance remain separate obligations.

The integrated pass matches all 256 words of context-four production attention
against exported PyTorch and pinned independent NumPy for one runtime QKV case,
covering all sixteen heads. Evidence is in
`artifacts/graph-full-attention-retirement`. Three native retirement tests cover
IR/control construction, idempotence, serialization and wrong-order rejection.
The regenerated production block passes native export and all 256 outputs
against both CPU references for zero and two embedding inputs at context four.
Evidence is in `artifacts/graph-block-retirement-comparison`. Complete-model
generation and FPGA acceptance remain pending.

`compiler.pipeline.bind_initialized_memories` declares validation, binding,
Futil and SV stages. Each receipt includes manifest, primitive and image hashes.
Production controller Calyx lowering, memory contracts, inventory, validation
and binding serialize native MLIR bytecode. The build profile records that
policy and each receipt hashes the actual bytecode artifact. This avoids
multi-gigabyte text expansion while preserving typed attributes and locations.
For source inspection, run `llm2fpga-calyx-opt artifact.mlirbc --verify-each -o
inspection.mlir`; the compiler performs the decoding into a separate artifact.
Native Calyx synthesis mode is explicit; assertion-enabled simulation remains
available through the default mode. Initialized GEMV replay uses captured PyTorch
weights across a caller/reference boundary and compares changed runtime operands
against CPU execution. These are kernel checks, not full-model acceptance.

The bounded disjointness serializer creates private helper wires for simulation
assertions. `graph-calyx-synthesis-check-dependencies.patch` gates construction
of those helpers and their checks on non-synthesis mode, matching the upstream
nested backend's omission of procedural assertions during synthesis. Otherwise
unused `$onehot0`/`$countones` helper wires still reach the synthesis frontend.
The 257-assignment synthesis regression covers this dependency boundary; the
25,000-guard simulation regressions retain the complete checks. This changes
native backend source, without editing emitted RTL or disabling verification.

The packaged `llm2fpga-calyx-translate` driver builds the pinned CIRCT emitter
with `nix/patches/graph-calyx-reference-primitive-export.patch`. A boolean
`llm2fpga.ref` attribute on a `calyx.primitive` selects the native `ref` keyword;
false preserves ordinary cell emission, and malformed flags fail export.
This is structured IR serialization in the compiler backend. The source patch
is applied before compilation. Four export tests include native Calyx frontend
validation across a caller/reference boundary. The Nix kernel check uses this
translator for its common graph route. Composed Q/DQ + GEMV replay passes;
the context-four production block now passes three runtime RTL comparisons.
Full-model RTL replay remains open.

The shared SV profiles apply native `cell-share:bounds=1,-1,-1`. Two exclusive
groups using subtraction and XOR in opposite orders reproduce a combinational
cycle under unrestricted sharing: schedule exclusivity does not prove that
the merged physical datapath is acyclic. The bound leaves the combinational
sharing class unmerged while retaining unrestricted register and state_share
classes. Both operator orders match all 256 byte inputs, and Yosys finds no SCC
in that regression. The production context-four block also changes from one
SCC to zero; see `artifacts/graph-combinational-sharing-qualification`. The new
policy requires its own block numerical replay and full-model/physical checks.

The graph-only Rust backend patches the pinned VAST concatenation serializer
to offer native line breaks. It also emits priority chains longer than 128
assignments through intermediate suffix wires. Each wire preserves the original
ternary order, default value and four-state behavior. A component-local name
generator reserves signature, cell, FSM and guard identifiers before emission.
This operates in the native backend; generated RTL is never rewritten.

A 25,000-guard regression retains the complete disjointness assertion and now
passes Verilator parsing. Yosys proves the old and new data paths equivalent for
512 overlapping guards. Assertions are retained in simulation; that equivalence
probe explicitly disables backend checks because Yosys cannot elaborate `$fatal`.
The shared Nix gate with the `scratch` stage passes 66 installed native tests and
12 runtime RTL comparisons, including initialized and composed kernels. The
subsequent three-case production-block replay passes; complete generation
remains a separate requirement.

The owned driver also compiles the pinned CIRCT `FlattenMemRefs.cpp` with
`graph-memref-row-major-flatten.patch`. Row-major flattening uses
`prefix = prefix * next_dimension + next_index`; the upstream suffix-product
recurrence addressed rank-three and rank-four buffers incorrectly. The shared
`flat-memory` stage runs this native pass and its canonicalizer through the
memory driver. Bounds-checked CPU regressions cover scalar through rank-four
allocations, calls, loads/stores and global initialization. Whole-model
qualification is recorded separately; these fixtures alone do not establish it.

## Exclusive pure-cell input lifting (candidate pass)

`llm2fpga-lift-exclusive-comb-inputs` moves unconditional input drives of a built-in pure combinational cell into continuous wires only when every input has one driver in the same dynamic group and every output use is an assignment inside that group. Sources must dominate the destination group. Stateful cells, external/protected/ref/runtime markers, guarded/shared inputs, escaping/control/done uses and group-local definitions are excluded. Analysis completes before moving connected chains; output and stateful assignments retain their group activation masks.

The pass is registered but not enabled in the common production profile. Seven native tests pass, including every byte input and changing idle inputs with identical before/after public RTL traces. Native installation checks also pass (88 memory, 62 Calyx tests and 256 four-state guard patterns). A new regression found externally observable/protected cells must be excluded; that RED-to-GREEN evidence is retained. Full-model applicability/resource effects and composed numerical qualification remain required before profile adoption. In particular, continuous sources may conservatively inhibit later native register sharing; resource reduction must be measured rather than assumed.

## Identical pure-cell deduplication (candidate pass)

`llm2fpga-deduplicate-identical-comb` merges only built-in combinational cells
with the same operation, properties (except instance name), result types and
exact continuously driven SSA inputs. Every input must have one unguarded
continuous assignment. Stateful cells, observation markers and named symbolic
observers are excluded. Native attribute traversal crosses nested symbol tables;
opaque metadata disables optimization for the component. Input assignments of
the duplicate are removed and output SSA uses are redirected to the survivor.
No operand-selection mux or new computation dependency is introduced. A stable
IR-order survivor and repeated passes handle reverse-declared dependency chains.

The pass remains outside the production profile. Native tests cover different
operands, guards, dependency chains, marker and symbolic observers, idempotence,
and identical public RTL traces for all 256 byte values plus X/Z idle values.
Full-model native applicability reduces 1,190 add and 1,252 left-shift declarations
before backend sharing; it leaves all 244 signed dividers unchanged. Those counts
are not mapped FPGA utilization or full-model numerical evidence. The separate
input-lifting pass supplied continuous inputs for this candidate measurement.

## Captured signed-divider transactions (candidate pass)

`llm2fpga-share-transactional-dividers` considers only the built-in signed
quotient and remainder pipe operations. Each candidate must have private,
unguarded operand drives in one dynamic group, canonical `go = !done`, exactly
one result use captured into a register in that group, and group completion
from that capture register. Divider completion cannot escape the transaction.
All activations must be under dynamic sequential/conditional/loop control;
parallel and static ancestors are excluded. Symbolic observers, protected/
external/ref/runtime markers and explicit divider or capture clock/reset drives
prevent reuse. Operations, properties and port types must match.

Before each selected group, an explicit retirement group waits for both divider
and capture-register completion to be low. Each group's result is captured
before another group can overwrite the shared divider. Port SSA uses are
redirected to a single same-type survivor; computation and operand expressions
remain intact. Name collisions are checked before any mutation, and the survivor
is marked so the pass is idempotent. Additional completion latency is allowed;
value/order equivalence is the contract, not cycle-by-cycle done equality.

The pass is not enabled in production. Initial native fixture checks preserve
parallel activation, escaping outputs and noncanonical completion; repeated
shared transactions match unshared RTL and CPU signed arithmetic across 1,280
cases. A capture-clock override regression found an eligibility omission; the
current source excludes those overrides and protected/observed capture registers.
Full-model requalification, resource measurement and numerical execution are
required before adoption. Merely adding state-sharing annotations to the library
was previously shown insufficient and remains unadopted.

## Static induction-state narrowing (candidate pass)

Read [StaticLoopCounters.cpp](StaticLoopCounters.cpp) alongside
[the native counter tests](../../../tests/test_graph_calyx_loop_counter_width.py).
`llm2fpga-narrow-static-loop-counters` changes the first carried value of
a canonical `scf.while`, before SCF-to-Calyx lowering. It requires single-block
before/after regions, unchanged condition forwarding, a direct signed or
unsigned less-than comparison, and an additive induction update. Initial value,
upper bound and positive step must be integer constants, with
`0 <= initial < upper`.

The conservative maximum is `upper + step - 1`, including the update that exits
the loop. It must fit the original signed capacity; index values use the pinned
SCF-to-Calyx route's signed 32-bit contract. The selected integer width retains a
zero sign bit. Only induction initialization, state, comparison and update become
narrower. Native casts/extensions restore the original type at other body uses
and the public loop result. Other carried values remain unchanged. Dynamic,
empty, negative, noncanonical and overflow-risk domains are skipped. No sampled
activation values or expected-result bounds participate.

The native fixture checks include index/i32/i64 execution, nonunit steps,
exit-value preservation, rejected domains and generated RTL execution. Complete
linked-IR CPU comparison also passes for both declared runtime prompts. The
pass remains outside the common production profile: emitted full-model RTL
numerical checks and mapped resource/structural checks remain required. Register
declaration savings are not an FPGA fit result.

## Register-fed captured pure computations (candidate pass)

Read [RegisterBoundedComb.cpp](RegisterBoundedComb.cpp) alongside
[the native capture tests](../../../tests/test_graph_calyx_registered_comb.py).
`llm2fpga-share-register-bounded-comb` considers built-in pure cells whose inputs
are constants or plain register outputs, driven unconditionally in one dynamic
group. A single result must be captured into a plain register in that group;
write-enable is literal true and group completion is that register's done.
Separate initialization writes are allowed only as group-local writes. Reading
capture input ports or writing them continuously prevents eligibility.

Matching cells in distinct, exclusively scheduled groups may share an instance.
Operation, properties and port types must match. Parallel/static activation,
combinational source chains, clock/reset overrides, protected/external/ref/runtime
markers, symbolic observers and opaque metadata prevent reuse. These register
boundaries avoid introducing a combinational dependency chain between reused
operations. Analysis uses the original IR; merged input ports make a survivor
ineligible for another merge on a repeated invocation.

Fixture CPU/RTL traces pass, but the whole-model candidate did not demonstrate a
resource benefit and its final flattening failed. It remains disabled and is
omitted from the separately measured static-counter candidate. Reducing operator
declarations does not account for the operand-selection mux cost.
