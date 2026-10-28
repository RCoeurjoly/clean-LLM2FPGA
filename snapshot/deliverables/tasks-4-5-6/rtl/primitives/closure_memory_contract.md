# Explicit memory primitive contract

`closure_memory.sv` is a compiler-owned source template. The emitter creates
three module definitions and a matching Calyx declaration before compiling
any model. It does not inspect or edit generated SystemVerilog.

Register memories acknowledge a request on its sampling edge. BRAM and
LUTRAM memories acknowledge on the following edge, with the sampled address
carried in a register. `done` identifies the corresponding accepted request;
`read_data` is valid for a read at that completion edge and remains stable
until another completed read or reset. Writes commit on the request sampling
edge, with completion at the declared latency. Repeated held requests repeat
the same memory operation; writes are idempotent when address/data remain
stable. These primitives must not serve side-effecting register interfaces.

Consecutive requests are supported. A queued read concurrent with a new write
samples the previous memory value at that edge (nonblocking read-before-write
semantics). Reset cancels queued completion and resets response registers,
but does not erase image or scratch storage. Caller scheduling owns scratch
initialization. Immutable instances prohibit writes in simulation and suppress
them in synthesis.

Every immutable memory binds a numeric `INIT_ID` to exactly one literal image
filename. The local initializer is emitted with the primitive and checked by
simulation and Yosys elaboration. Missing, malformed, truncated, over-width or
unsafe image bindings fail before building the image. Width/depth records are
validated against the compiler's logical memory specification.

The previous private postpass stretched all sequential-memory schedules and
changed selected RTL implementations afterward. The new two-cycle primitive
queues the read request itself, so an enable pulse is handled correctly. This
is a protocol change requiring full caller simulation and new physical-image
validation when integrated. Primitive tests alone do not establish full-model
or board equivalence. The active production pipeline still uses its historical
postpasses until the structured-lowering integration is complete.
