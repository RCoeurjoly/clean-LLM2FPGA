/-
Word-level stratum, task 6 of 7 (HWMCC'24 word-level instance 1/2).

Source: HWMCC'24 (Zenodo DOI 10.5281/zenodo.14156844, CC-BY-4.0), file
`artifacts/hwmcc24/btor2_bv/btor2/2024/hku/arithmetic_circuits/fib_30/fib_30.btor2`
(39 lines, the single smallest file in the 319-instance `benchmarks_btor2_bv`
corpus by line count; sha256 1bb400e569c8c3147d38245dd81458261c4cc0aac565286e731dabf78fdf31d0).

Node-by-node transcription of every `state`/`init`/`next`/`bad` node in the
file (word width 19 bits throughout, per the file's `sort bitvec 19`):
state `c` (btor id 7, init 0), state `i` (id 10, init 0), state `n` (id 14,
init the file's literal constant `0000000000010010110` = decimal 150).
`next`(c) = id 28/29, `next`(i) = id 33/34, `next`(n) = id 35/36, `bad` =
id 24. Input `rst` (id 3) is the only input those formulas reference;
inputs `clk` (id 2) and `selector` (id 4) are declared in the file but do
not appear in any state/bad formula's cone of influence (verified by
grepping every node argument list for ids 2 and 4) so they are correctly
omitted from this transcription.

**Deviation from a literal reading of "bound to depth 1", logged per the
STATABILITY-FIRST discipline (see HardBench/WordLevel/STATABILITY.md for
the full discussion):** no instance in `benchmarks_btor2_bv` is purely
combinational (every file has `state`/`next` nodes), and this instance's
*competition* property (`bad` reachable at all) genuinely requires
unrolling until `i` counts up to `n` = 150 -- not depth 1. We therefore
prove the literal, faithful thing "bound to depth 1" can mean for a real
sequential instance: `bad` does not hold at the initial state (k=0) nor at
the state reached by exactly one application of the real transcribed
`next` relation (k=1), for every value of the only free input (`rst`) that
formula depends on. This is a genuine, real BMC-style safety-up-to-bound-1
claim about the actual instance, not a claim about the unbounded
competition property.
-/

import Std.Tactic.BVDecide

namespace HardBench.WordLevel

/-- The 3 word-level state registers of `fib_30.btor2` (all 19-bit, per the
    file's `sort bitvec 19`). -/
structure Fib30State where
  c : BitVec 19
  i : BitVec 19
  n : BitVec 19

/-- `init` nodes 8 (`c`), 11 (`i`), 15 (`n`, to the file's literal constant
    `0000000000010010110` = 150). -/
def fib30Init : Fib30State := { c := 0#19, i := 0#19, n := 150#19 }

/-- `next` nodes 28/29 (`c`), 33/34 (`i`), 35/36 (`n`), transcribed directly
    from the btor2 `ite` chains (reset-then-saturating-accumulate). -/
def fib30Step (s : Fib30State) (rst : BitVec 1) : Fib30State :=
  { c := if rst = 1#1 then 0#19 else (if s.i ≥ s.n then s.c else s.c + s.i)
    i := if rst = 1#1 then 0#19 else (if s.i ≥ s.n then s.i else s.i + 1#19)
    n := if rst = 1#1 then 150#19 else s.n }

/-- `bad` node 24: `(i >= n) and (c == 0)`. -/
def fib30Bad (s : Fib30State) : Prop := s.i ≥ s.n ∧ s.c = 0#19

/-- Safety up to bound 1 (see file header for why bound 1, not the full
    unbounded competition property): `bad` holds neither at the initial
    state nor after one real transition, for every possible `rst`. -/
theorem fib30_bound1_safe (rst : BitVec 1) :
    ¬ fib30Bad fib30Init ∧ ¬ fib30Bad (fib30Step fib30Init rst) := by
  constructor <;> simp only [fib30Bad, fib30Init, fib30Step] <;> bv_decide

end HardBench.WordLevel
