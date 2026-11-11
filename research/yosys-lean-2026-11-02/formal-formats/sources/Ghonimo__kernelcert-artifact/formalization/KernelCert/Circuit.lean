/-
  KernelCert.Circuit — the sequential-circuit model and constrained-trace safety.

  Mirrors the manuscript's section "The Formalized Framework in Lean 4". To keep this
  layer self-contained, the reset/transition/property/constraint predicates are
  given *semantically* (as Lean functions) instead of as denotations of a
  `Std.Sat.AIG`: in a faithful refinement each would be `⟦·⟧ρ` over the shared AIG
  gate pool. The `SeqCircuit` fields themselves can later be instantiated by AIG
  denotations without changing THIS file's statements — but the check predicates and
  stratification WILL change statement in the faithful syntactic refinement (see the
  header of KernelCert/Witness.lean), so the wiring is not purely mechanical across
  the whole development.
-/
namespace KernelCert

/-- A bit-level sequential circuit `M = (I, L, R, F, P, C)`.
    `resetVal`, `next`, `good`, `cons` stand in for the AIG denotations of the reset
    functions, next-state functions, good-states predicate `P`, and constraint `C`. -/
structure SeqCircuit where
  /-- number of ordered inputs `I`. -/
  nI : Nat
  /-- number of ordered latches `L`. -/
  nL : Nat
  /-- per-latch reset value as a function of the current latch state.
      Reset logic depends on latches only (the paper's well-formedness condition),
      which is why the reset-dependency graph is a latch-to-latch notion. -/
  resetVal : (Fin nL → Bool) → Fin nL → Bool
  /-- per-latch next-state function of `(inputs, current state)`. -/
  next : (Fin nI → Bool) → (Fin nL → Bool) → Fin nL → Bool
  /-- good-states predicate `P` (negation of the disjunction of bad/output signals). -/
  good : (Fin nI → Bool) → (Fin nL → Bool) → Bool
  /-- AIGER 1.9 constraint predicate `C`. -/
  cons : (Fin nI → Bool) → (Fin nL → Bool) → Bool

/-- A state assigns a Boolean to every latch. -/
abbrev State (M : SeqCircuit) := Fin M.nL → Bool
/-- An input valuation assigns a Boolean to every input. -/
abbrev Input (M : SeqCircuit) := Fin M.nI → Bool

/-- Reset predicate `R`: every latch already carries its reset value. -/
def R (M : SeqCircuit) (s : State M) : Prop :=
  ∀ l, s l = M.resetVal s l

/-- Two-state transition predicate `F`: every post-latch equals its next-state
    function evaluated on the pre-configuration. -/
def F (M : SeqCircuit) (ι : Input M) (s s' : State M) : Prop :=
  ∀ l, s' l = M.next ι s l

/-- Property evaluated at a configuration. -/
def evalP (M : SeqCircuit) (ι : Input M) (s : State M) : Bool := M.good ι s

/-- Constraint evaluated at a configuration. -/
def evalC (M : SeqCircuit) (ι : Input M) (s : State M) : Bool := M.cons ι s

/-- A **constrained run** of length `n`: a state/input sequence that starts in a reset
    state, respects the transition relation, and satisfies the constraint at every
    visited configuration. -/
structure ConstrainedRun (M : SeqCircuit) (n : Nat) where
  st : Nat → State M
  inp : Nat → Input M
  reset0 : R M (st 0)
  cons_ok : ∀ i, i ≤ n → evalC M (inp i) (st i) = true
  trans_ok : ∀ i, i < n → F M (inp i) (st i) (st (i + 1))

/-- **Safety**: every constrained run, of every length, ends in a good state.
    This is the conclusion the paper's central theorem produces. -/
def Safe (M : SeqCircuit) : Prop :=
  ∀ n (τ : ConstrainedRun M n), evalP M (τ.inp n) (τ.st n) = true

/-- A **bad trace** of length `n`: a constrained run whose final configuration
    falsifies `P`. The length-indexed contrapositive of `Safe`. -/
def BadTrace (M : SeqCircuit) (n : Nat) : Prop :=
  ∃ τ : ConstrainedRun M n, evalP M (τ.inp n) (τ.st n) = false

/-- Elementary equivalence used to turn the metatheorem into an induction on trace
    length: safety is exactly the absence of bad traces of every length. -/
theorem safe_iff_no_bad (M : SeqCircuit) :
    Safe M ↔ ∀ n, ¬ BadTrace M n := by
  constructor
  · intro hs n hbad
    obtain ⟨τ, hb⟩ := hbad
    have ht := hs n τ
    rw [ht] at hb
    exact Bool.noConfusion hb
  · intro h n τ
    cases hp : evalP M (τ.inp n) (τ.st n) with
    | true => rfl
    | false => exact absurd ⟨τ, hp⟩ (h n)

end KernelCert
