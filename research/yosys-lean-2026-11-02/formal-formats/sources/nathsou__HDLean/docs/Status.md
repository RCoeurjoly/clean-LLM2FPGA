# Status

Current state of the implementation. The design and its milestones are in
[Design.md](Design.md); implementation choices in [Decisions.md](Decisions.md);
what is trusted in [Trust.md](Trust.md).

## Pipeline

One language, one path to bits. Designs are written in the word-level RTL
(`HDLean/Rtl`, [Design.md](Design.md) 3.1): typed records, a checked builder,
memories, module instances, and several clock domains joined by two-flop
synchronizers and shared memories (`MDesign`). Single-clock designs print as
word-level SystemVerilog and RTLIL (proved round trips), pass through the proved
word-level passes (`Design.optimized`: rewriting, then dead-cell removal), and
reach the bit-level backends through the proved bit-blaster `Design.toDag`
(`toDag_nextState`, `toDag_evalOutput`). The bit-level node graph (`Dag`) is
lowered by `Dag.lowerTight` and optimised by the proved AIG pass `Graph.optimize`;
the printers are proved by parse-after-print round trips, so `Proved.emit_correct`
gives the meaning of every emitted file. `hdlean abc-emit` adds ABC's optimisation,
accepted only with an LRAT certificate checked by Lean's verified checker
([Design.md](Design.md) 13.5). The CLI reads each emitted file back with the
proved parser.

Multi-clock designs print as word-level SystemVerilog and RTLIL with proved round
trips (`MDesign.parse_systemVerilog`, `MDesign.parse_rtlil`).

All proofs are checked by Lean's official kernel. There is no `sorry`, and every
audited theorem uses only `propext`, `Classical.choice` and `Quot.sound`.

## Designs and their challenges

| Design | Challenge (frozen, readable) | Implementation |
| --- | --- | --- |
| Counter | Decade behaviour after one reset cycle from any power-up store; 4 state bits | One Mealy module, one register |
| Two-entry FIFO | The capacity-2 queue after reset, every positive payload width; `2w+2` state bits | Count and two slots; ready and valid from the count |
| General-depth FIFO | The capacity-`2^k` queue after reset, every positive `k` and width; `2k+1` register bits and `2^k` words of memory | Head and count registers, payload in an RTL memory; implements the circular-buffer model cycle for cycle |
| Three-stage pipeline | Three coupled queues after reset (every stage's ports observed); `3(2w+2)` state bits | One module; the FIFO's stage functions used three times |
| Single-port RAM | Read port follows the read-before-write RAM for some initial state, any inputs and resets | One RTL memory, one write port |
| Toy16 CPU | Retirement refines the ISA after one reset cycle from any power-up store; 2-cycle progress | Register file and data memory as RTL memories, ALU as an instantiated module |
| Dual-clock FIFO | Queue trace after a reset startup for every power-up state, clock interleaving and sampling oracle; writes and reads complete within three fair windows | Two clock domains with Gray pointers exchanged through synchronizers and a shared memory; implements the dual-clock model exactly |

`scripts/verify.py` audits 59 theorems: the 23 challenge theorems, the two
challenges re-proved for the optimised designs, and 34 generic framework theorems
(emitter, lowerings, AIG optimisation, validated emission, word-level passes,
refinement rules, bit-blasting, word-level and multi-clock round trips, builder,
memory, instance and register-cell rules, and the two model correspondences).
[Review.md](Review.md) is generated from them by `hdlean review`.

## Checks

- `lake build` and `leanchecker --fresh Designs` (official kernel).
- Challenge audit: axioms, and statement constants from frozen modules or declared holes.
- `lake exe tests`: every single-clock design against its reference on random
  traces with resets (900 counter, 2,700 FIFO, 2,700 pipeline and 8,100
  general-depth FIFO cycles), the CPU's retirements against the ISA, 7,200
  dual-clock steps of the multi-clock design against the dual-clock model (state
  and ports), bit-blasted graphs against their lowered gate graphs, emitter
  read-back and mutation rejection.
- `scripts/check_backends.py`: for every design, AIGER interpretation, 100 Icarus
  cycles and Yosys RTLIL/SystemVerilog equivalence of the bit-blasted netlist (a
  corrupted next state must fail); Icarus runs the word SystemVerilog, the word
  RTLIL (through Yosys) and the bit-blasted netlist side by side against 200 cycles
  of the Lean trace of `Design.step`; Yosys elaborates the word-level text
  (hierarchy, memories). For the counter, the general-depth FIFO and the RAM,
  `abc-emit` must be validated, match the trace and be Yosys-equivalent to the
  plain emission.
- `scripts/check_native_clocks.py`: the dual-clock FIFO's word SystemVerilog and
  word RTLIL (through Yosys) run in Icarus with two real clocks against 400-step
  Lean traces of `MDesign.next`, at two widths and two seeds.

## Emitted sizes (bit-level formats, width 8)

| Design | State bits | AND gates, emitted | Logic depth | AND gates, `abc-emit` (validated) |
| --- | --- | --- | --- | --- |
| Counter | 4 | 32 | 8 | 20 |
| Two-entry FIFO | 18 | 118 | 10 | 115 |
| Pipeline | 54 | 352 | 10 | 343 |
| General-depth FIFO (16 × 8) | 137 | 892 | 16 | 867 |
| RAM (16 × 8) | 136 | 788 | 8 | 710 |
| Toy16 CPU | 4,312 | 27,764 | 53 | 25,164 |

"Emitted" is the proved bit-blaster, lowering and AIG pass (what `emit` writes and
`metrics` reports). Memories are counted as state bits here and stay memories in
the word-level text. No retiming; synthesis improves on these numbers. Physical
measurements are run on demand ([Physical.md](Physical.md)) and not committed.

## Known gaps

- Multi-clock designs have no bit-level backend: they are emitted as word
  SystemVerilog and RTLIL only. Their top module's structure is decoded from its
  header comments; the rest of it is checked by exact reprint, so Yosys and Icarus
  runs are its independent check.
- Budgets exist for single-clock designs only (`Blast.stateBits`); there is no state
  measure for multi-clock designs yet.
- The example designs have little for the word-level passes to rewrite or remove;
  `Tests/Backend.lean` exercises the passes on a redundant module.
- No sequential optimisation (retiming, register merging, state re-encoding):
  deferred, see [Design.md](Design.md) 13.5.
- Exact emitted bytes are not kernel-checked; the proved-parser read-back replaces it.
