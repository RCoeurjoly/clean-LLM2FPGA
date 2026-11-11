-- Copyright (c) 2026 Kevin Baragona
-- SPDX-License-Identifier: Apache-2.0
import Loom.Hw.Syntax
import Loom.Core.Ts
import Loom.Core.Word

/-!
# EDSL semantics (L3)

One cycle: evaluate every rule against the *pre-cycle* state, accumulating
a write log; commit the log. Deterministic and total; the `TSys` instance
is the object the refinement proofs (A-R, R-MC) and the L2 engines talk
about, and the netlist compiler (C-HW) must preserve.
-/

namespace Loom.Hw

/-- Register valuation (name/width-indexed, like µVerilog's `Env`). -/
def RegEnv := String → (w : Nat) → BitVec w

/-- Memory contents. -/
def MemEnv := String → Nat → (w : Nat) → BitVec w

/-- Design state. -/
structure St where
  regs : RegEnv
  mems : MemEnv

def RegEnv.set (ρ : RegEnv) (name : String) {w : Nat} (v : BitVec w) : RegEnv :=
  fun n w' => if n = name then (if h : w = w' then h ▸ v else ρ n w') else ρ n w'

/-- Read back a register write at the coordinate just written. -/
@[simp] theorem RegEnv.set_read_self (ρ : RegEnv) (name : String)
    {w : Nat} (v : BitVec w) : (ρ.set name v) name w = v := by
  simp [RegEnv.set]

/-- A register write preserves every differently named coordinate. -/
@[simp] theorem RegEnv.set_read_other (ρ : RegEnv) (written : String)
    {wv : Nat} (v : BitVec wv) (name : String) (width : Nat)
    (different : name ≠ written) :
    (ρ.set written v) name width = ρ name width := by
  simp [RegEnv.set, different]

/-- A memory write touches exactly the written `(name, addr, width)` entry;
entries at other widths are junk (unobservable at declared widths) and are
preserved, mirroring `RegEnv.set` and the µVerilog write-port semantics. -/
def MemEnv.set (μ : MemEnv) (name : String) (a : Nat) {w : Nat} (v : BitVec w) :
    MemEnv :=
  fun n a' w' =>
    if n = name ∧ a' = a then (if h : w = w' then h ▸ v else μ n a' w')
    else μ n a' w'

/-- Evaluate an expression against the pre-cycle state. Total. -/
def Expr.eval (σ : St) : {w : Nat} → Expr w → BitVec w
  | _, .lit v => v
  | w, .reg _ n => σ.regs n w
  | dw, .memRead _ m addr => σ.mems m (addr.eval σ).toNat dw
  | _, .and a b => a.eval σ &&& b.eval σ
  | _, .or a b => a.eval σ ||| b.eval σ
  | _, .xor a b => a.eval σ ^^^ b.eval σ
  | _, .not a => ~~~(a.eval σ)
  | _, .add a b => a.eval σ + b.eval σ
  | _, .sub a b => a.eval σ - b.eval σ
  | _, .mul a b => a.eval σ * b.eval σ
  | _, .udiv a b => a.eval σ / b.eval σ
  | _, .urem a b => a.eval σ % b.eval σ
  | _, .shl a b => a.eval σ <<< (b.eval σ).toNat
  | _, .shr a b => a.eval σ >>> (b.eval σ).toNat
  | _, .eq a b => if a.eval σ = b.eval σ then 1#1 else 0#1
  | _, .ult a b => if (a.eval σ).ult (b.eval σ) then 1#1 else 0#1
  | _, .slt a b => if (a.eval σ).slt (b.eval σ) then 1#1 else 0#1
  | _, .mux c t f => if c.eval σ = 1#1 then t.eval σ else f.eval σ
  | _, .slice a lo width => (a.eval σ).extractLsb' lo width
  | _, .zext a w' => (a.eval σ).setWidth w'
  | _, .sext a w' => (a.eval σ).signExtend w'

/-- The `concat` smart constructor evaluates to literal bit-vector append.
This small semantic bridge is useful to proofs over packed records and does
not add a compiler case because `concat` already lowers to shifts and OR. -/
theorem Expr.concat_eval {hi lo : Nat} (msbs : Expr hi) (lsbs : Expr lo)
    (state : St) :
    (Expr.concat msbs lsbs).eval state = msbs.eval state ++ lsbs.eval state := by
  cases lo with
  | zero =>
      simp [Expr.concat]
  | succ lo =>
      apply BitVec.eq_of_getLsbD_eq
      intro index inBounds
      have loSmall : lo + 1 < 2 ^ (hi + (lo + 1)) := by
        have power : hi + (lo + 1) < 2 ^ (hi + (lo + 1)) :=
          Nat.lt_two_pow_self
        omega
      simp only [Expr.concat, Expr.eval, BitVec.getLsbD_or,
        BitVec.getLsbD_shiftLeft, BitVec.getLsbD_setWidth,
        BitVec.getLsbD_append, BitVec.toNat_ofNat, Nat.mod_eq_of_lt loSmall]
      by_cases low : index < lo + 1
      · have lowHi : index < hi + (lo + 1) := by omega
        simp [low, lowHi]
      · have lowGe : lo + 1 ≤ index := by omega
        have highIndex : index - (lo + 1) < hi := by omega
        have totalIndex : index - (lo + 1) < hi + (lo + 1) := by omega
        simp [low, inBounds, lowGe, highIndex, totalIndex]

/-- Run an action: reads from the pre-cycle state `σ`, writes onto the
accumulator `acc` (last write wins — D9). -/
def Act.run (σ : St) : Act → St → St
  | .skip, acc => acc
  | .seq a b, acc => b.run σ (a.run σ acc)
  | .ite c t e, acc => if c.eval σ = 1#1 then t.run σ acc else e.run σ acc
  | .write _ r v, acc => { acc with regs := acc.regs.set r (v.eval σ) }
  | .writeSlice totalWidth r lo _ _ v, acc =>
      let next := Loom.Word.insert lo (v.eval σ) (acc.regs r totalWidth)
      { acc with regs := acc.regs.set r next }
  | .memWrite _ _ m _ addr data, acc =>
      { acc with mems := acc.mems.set m (addr.eval σ).toNat (data.eval σ) }

/-- One cycle of a design. -/
def Design.cycle (d : Design) (σ : St) : St :=
  d.rules.foldl (fun acc r => r.body.run σ acc) σ

/-- The reset state. -/
def Design.reset (d : Design) : St where
  regs := d.regs.foldl (fun ρ r => ρ.set r.name r.init) (fun _ w => 0#w)
  mems := d.mems.foldl
    (fun μ m => fun n a w =>
      if n = m.name ∧ w = m.dataWidth ∧ a < 2 ^ m.addrWidth then
        (m.init a).setWidth w
      else μ n a w)
    (fun _ _ w => 0#w)

/-- One clock edge with the synchronous reset pin made explicit. Reset has
priority over the ordinary transition, matching the emitted µVerilog module.
This is useful for live-reset composition without changing the reset-free
`Design.cycle` used by ordinary island proofs. -/
def Design.cycleWithReset (d : Design) (reset : Bool) (σ : St) : St :=
  if reset then d.reset else d.cycle σ

/-- Run `n` cycles. -/
def Design.run (d : Design) : Nat → St → St
  | 0, σ => σ
  | n + 1, σ => d.run n (d.cycle σ)

/-- A design as a transition system (P2): the concrete side of A-R/R-MC
and the object C-HW preserves. -/
def Design.toTSys (d : Design) : Loom.TSys :=
  Loom.TSys.ofFun St (fun σ => σ = d.reset) d.cycle

/-- The init predicate of a design's transition system is exactly "the state
is the reset state". Tagged `@[simp]` so invariant proofs can `simp at hinit`
instead of the `have : s = d.reset := hinit; subst this` two-step
(tutorial defect #4). -/
@[simp] theorem Design.toTSys_init_iff (d : Design) (σ : St) :
    d.toTSys.init σ ↔ σ = d.reset := Iff.rfl

/-- One step of a design's transition system is one `cycle`. -/
@[simp] theorem Design.toTSys_step_iff (d : Design) (σ σ' : St) :
    d.toTSys.step σ σ' ↔ d.cycle σ = σ' := Iff.rfl

/-! ## Open designs (D15)

Inputs are environment-owned register coordinates: between clock edges the
environment drives the input pins, and at the edge the design's rules read
them like any pre-cycle state (the pin value at the moment of the edge).
Nothing in `Expr`/`Act`/`cycle` changes — an open cycle is the closed
cycle of the input-poked state. -/

/-- Input valuation: what the environment drives on the input ports. -/
def InEnv := String → (w : Nat) → BitVec w

/-- Overwrite the input coordinates of a state from a valuation. -/
def St.setInputs (σ : St) (ins : List InputDecl) (ι : InEnv) : St :=
  { σ with regs := ins.foldl (fun ρ i => ρ.set i.name (ι i.name i.width)) σ.regs }

/-- Observe one declared combinational output during a clock interval.
Inputs are installed exactly as they are for `cycleOpen`, but no transition
is taken: the value is a pure function of the current inputs and pre-edge
state. After an edge, observing the resulting state naturally exposes the
post-edge value. -/
def Design.evalCombOutput (d : Design) (ι : InEnv) (σ : St)
    (output : CombOutput) : BitVec output.width :=
  output.value.eval (σ.setInputs d.inputs ι)

/-- Installing inputs preserves a register coordinate absent from the input
declaration list. Width is part of the coordinate, matching `RegEnv.set`. -/
theorem St.setInputs_regs_notin (σ : St) (ins : List InputDecl) (ι : InEnv)
    (name : String) (width : Nat)
    (absent : ∀ input ∈ ins, (name, width) ≠ (input.name, input.width)) :
    (σ.setInputs ins ι).regs name width = σ.regs name width := by
  show (ins.foldl (fun ρ input =>
    ρ.set input.name (ι input.name input.width)) σ.regs) name width = _
  suffices preserve : ∀ (inputs : List InputDecl) (ρ : RegEnv),
      (∀ input ∈ inputs, (name, width) ≠ (input.name, input.width)) →
      (inputs.foldl (fun acc input =>
        acc.set input.name (ι input.name input.width)) ρ) name width =
        ρ name width by
    exact preserve ins σ.regs absent
  intro inputs
  induction inputs with
  | nil => intro ρ _; rfl
  | cons input rest ih =>
      intro ρ h
      simp only [List.foldl_cons]
      rw [ih _ (fun member present => h member (List.mem_cons_of_mem _ present))]
      unfold RegEnv.set
      by_cases hname : name = input.name
      · rw [if_pos hname]
        by_cases hwidth : input.width = width
        · exact False.elim <| h input (List.mem_cons_self) (by
            simp [hname, hwidth])
        · rw [dif_neg hwidth]
      · rw [if_neg hname]

/-- One cycle of an open design: the environment drives the inputs, the
design cycles. For a closed design this is `cycle`. -/
def Design.cycleOpen (d : Design) (ι : InEnv) (σ : St) : St :=
  d.cycle (σ.setInputs d.inputs ι)

/-- Open-design edge with synchronous reset. Inputs are irrelevant while
reset is asserted because the generated reset branch has priority. -/
def Design.cycleOpenWithReset (d : Design) (reset : Bool)
    (ι : InEnv) (σ : St) : St :=
  if reset then d.reset else d.cycleOpen ι σ

/-- Run under an input trace (`ιs k` drives cycle `k`). -/
def Design.runOpen (d : Design) (ιs : Nat → InEnv) : Nat → St → St
  | 0, σ => σ
  | n + 1, σ => d.runOpen (fun k => ιs (k + 1)) n (d.cycleOpen (ιs 0) σ)

/-! ## Open systems with explicit environment assumptions

An open-core safety claim must say which input valuations the environment may
supply.  Keeping that predicate in the transition relation prevents a host or
board protocol from becoming an invisible premise of a theorem.
-/

/-- A state-dependent contract on the next environment input. -/
abbrev InputAssumption := St → InEnv → Prop

/-- Open-design transition system restricted to inputs satisfying `assume`.
The witness remains part of every step, so the assumption is explicit at each
cycle rather than attached as prose to an unrestricted open system. -/
def Design.toAssumedOpenTSys (d : Design) (assume : InputAssumption) :
    Loom.TSys where
  S := St
  init := fun σ => σ = d.reset
  step := fun σ τ => ∃ ι, assume σ ι ∧ d.cycleOpen ι σ = τ

@[simp] theorem Design.toAssumedOpenTSys_init_iff (d : Design)
    (assume : InputAssumption) (σ : St) :
    (d.toAssumedOpenTSys assume).init σ ↔ σ = d.reset := Iff.rfl

@[simp] theorem Design.toAssumedOpenTSys_step_iff (d : Design)
    (assume : InputAssumption) (σ τ : St) :
    (d.toAssumedOpenTSys assume).step σ τ ↔
      ∃ ι, assume σ ι ∧ d.cycleOpen ι σ = τ := Iff.rfl

/-- Assume/guarantee induction for an open design. -/
theorem Design.invariant_of_assumedCycleOpen (d : Design)
    (assume : InputAssumption) (property : St → Prop)
    (reset : property d.reset)
    (step : ∀ σ ι, property σ → assume σ ι → property (d.cycleOpen ι σ)) :
    (d.toAssumedOpenTSys assume).Invariant property := by
  apply Loom.TSys.Inductive.invariant
  constructor
  · intro σ initial
    subst σ
    exact reset
  · intro σ τ current transition
    obtain ⟨ι, accepted, rfl⟩ := transition
    exact step σ ι current accepted

/-- A closed invariant is also an arbitrary-input invariant when the Design
declares no inputs. This is the common bridge for placing an existing closed
synchronous block into a named System without changing its proof. -/
theorem Design.openInvariant_of_noInputs (d : Design) {property : St → Prop}
    (noInputs : d.inputs = []) (invariant : d.toTSys.Invariant property) :
    (d.toAssumedOpenTSys (fun _ _ => True)).Invariant property := by
  intro state reachable
  apply invariant state
  induction reachable with
  | init initial =>
      apply Loom.TSys.Reachable.init
      simpa [Design.toTSys_init_iff] using initial
  | step reachable transition ih =>
      rename_i before after
      rcases transition with ⟨input, _, next⟩
      apply Loom.TSys.Reachable.step ih
      simp only [Design.toTSys_step_iff]
      rw [← next]
      unfold Design.cycleOpen
      rw [noInputs]
      have unchanged : before.setInputs [] input = before := by
        cases before
        rfl
      rw [unchanged]

end Loom.Hw
