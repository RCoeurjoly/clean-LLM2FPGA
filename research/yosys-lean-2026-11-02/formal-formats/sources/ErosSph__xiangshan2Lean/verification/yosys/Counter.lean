import Yosys.Counter4
import Std.Tactic.BVDecide

set_option maxRecDepth 65536
set_option maxHeartbeats 0

namespace Yosys.CounterProof
open Yosys.Counter4

-- Match the public cycle driver: input changes at low clock, then a rising edge.
def cycle (i : Inputs) (s : State) : Evaluation :=
  let low := evaluate { i with i1 := 0 } s
  evaluate { i with i1 := 1 } low.nextState

def expected (i : Inputs) (q : BitVec 4) : BitVec 4 :=
  if i.i0 == 1 then 0 else if i.i2 == 1 then q + 1 else q

theorem cycle_clock (i : Inputs) (s : State) :
    (cycle i s).nextState.s0 = 1 := by
  rfl

-- Includes arbitrary counter values, sampled data and the explicit unknown input.
theorem cycle_counter (i : Inputs) (s : State) (h : s.s0 = 1) :
    (cycle i s).nextState.s2 = expected i s.s2 := by
  unfold cycle evaluate expected
  simp only [h]
  bv_decide

theorem cycle_output (i : Inputs) (s : State) (h : s.s0 = 1) :
    (cycle i s).outputs.o0 = expected i s.s2 := by
  unfold cycle evaluate expected
  simp only [h]
  bv_decide

def run : List Inputs → State → State
  | [], s => s
  | i :: rest, s => run rest (cycle i s).nextState

def runSpec : List Inputs → BitVec 4 → BitVec 4
  | [], q => q
  | i :: rest, q => runSpec rest (expected i q)

theorem arbitrary_finite_sequences (xs : List Inputs) (s : State) (h : s.s0 = 1) :
    (run xs s).s2 = runSpec xs s.s2 := by
  induction xs generalizing s with
  | nil => rfl
  | cons i xs ih =>
    simp only [run, runSpec]
    rw [ih _ (cycle_clock i s), cycle_counter i s h]

#print axioms cycle_counter
#print axioms cycle_output
#print axioms arbitrary_finite_sequences
end Yosys.CounterProof
