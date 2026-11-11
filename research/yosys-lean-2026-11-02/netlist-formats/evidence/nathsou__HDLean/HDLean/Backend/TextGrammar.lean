import HDLean.Backend.TextCodec

set_option backward.isDefEq.respectTransparency false
namespace HDLean.Backend.Text

/-- A compositional grammar with a prefix-decoding theorem. -/
structure Grammar (printed token α : Type) (view : printed → token) where
  encode : α → List printed
  decode : List token → Option (α × List token)
  roundtrip : ∀ a tail, decode ((encode a).map view ++ tail) = some (a, tail)

namespace Grammar
variable {p t α β : Type} {view : p → t}

def pair (a : Grammar p t α view) (b : Grammar p t β view) : Grammar p t (α × β) view where
  encode x := a.encode x.1 ++ b.encode x.2
  decode ts := do
    let (x, ts) ← a.decode ts
    let (y, ts) ← b.decode ts
    return ((x,y),ts)
  roundtrip x tail := by
    simp only [List.map_append, List.append_assoc, a.roundtrip]
    dsimp only [Bind.bind, Option.bind]
    rw [b.roundtrip]
    rfl

/-- A retraction changes the grammar's representation without changing its text. -/
def map (g : Grammar p t α view) (f : α → β) (back : β → α)
    (h : ∀ b, f (back b) = b) : Grammar p t β view where
  encode b := g.encode (back b)
  decode ts := do
    let (a,ts) ← g.decode ts
    return (f a,ts)
  roundtrip b tail := by
    rw [g.roundtrip]
    dsimp only [Bind.bind, Option.bind]
    rw [h]
    rfl

/-- Reject representations that lose information under a retraction. This is
needed for native names and dimensions: odd literal targets and zero-width
padding must not silently decode to a different physical wire or width. -/
def checkedMap [DecidableEq α] (g : Grammar p t α view) (f : α → β) (back : β → α)
    (h : ∀ b, f (back b) = b) : Grammar p t β view where
  encode b := g.encode (back b)
  decode ts := do
    let (a,ts) ← g.decode ts
    if back (f a) = a then return (f a,ts) else none
  roundtrip b tail := by
    rw [g.roundtrip]
    dsimp only [Bind.bind,Option.bind]
    rw [h]
    simp

def fixed [DecidableEq t] (words : List p) : Grammar p t Unit view where
  encode _ := words
  decode ts := if (words.map view).isPrefixOf ts then some ((), ts.drop words.length) else none
  roundtrip a tail := by
    cases a
    simp [List.isPrefixOf_iff_prefix, List.prefix_append]

abbrev Row (α : Type) := Grammar Word String α Word.text

def natural : Row Nat where
  encode n := [numeral n]
  decode ts := match ts with
    | [] => none
    | t::ts => do let n ← t.toNat?; return (n,ts)
  roundtrip n tail := by simp

/-- Safe numbered HDL identifiers with a generic lexical round-trip proof. -/
def numbered (pre : Word) : Row Nat where
  encode n := [prefixed pre n]
  decode ts := match ts with
    | [] => none
    | word::rest => do
      let n ← parsePrefixed pre.text word
      if word = (prefixed pre n).text then pure (n,rest) else none
  roundtrip n tail := by simp [Word.text,prefixed_parse]

def before [DecidableEq t] (words : List p) (g : Grammar p t α view) : Grammar p t α view :=
  (pair (fixed words) g).map Prod.snd (fun a => ((),a)) (by intro a; rfl)

def after [DecidableEq t] (g : Grammar p t α view) (words : List p) : Grammar p t α view :=
  (pair g (fixed words)).map Prod.fst (fun a => (a,())) (by intro a; rfl)

/-- Lift a nonempty word grammar to a one-line file grammar. -/
def line (g : Row α) (hne : ∀ a, g.encode a ≠ []) :
    Grammar Line (List String) α Line.tokens where
  encode a := [⟨g.encode a, hne a⟩]
  decode ts := match ts with
    | [] => none
    | words::rest => do
      let (a, leftover) ← g.decode words
      if leftover.isEmpty then return (a,rest) else none
  roundtrip a tail := by
    simp only [List.map_cons, List.map_nil, List.cons_append, Line.tokens]
    have h := g.roundtrip a []
    simp only [List.append_nil] at h
    rw [h]
    rfl

/-- Repeated blocks retain their explicit allocation index. -/
def repeatEncode (g : Nat → Grammar p t α view) : Nat → List α → List p
  | _, [] => []
  | base, a::as => (g base).encode a ++ repeatEncode g (base+1) as

def repeatDecode (g : Nat → Grammar p t α view) :
    Nat → Nat → List t → Option (List α × List t)
  | 0, _, ts => some ([],ts)
  | n+1, base, ts => do
    let (a,ts) ← (g base).decode ts
    let (as,ts) ← repeatDecode g n (base+1) ts
    return (a::as,ts)

theorem repeat_roundtrip (g : Nat → Grammar p t α view) (as : List α) (base : Nat) (tail : List t) :
    repeatDecode g as.length base ((repeatEncode g base as).map view ++ tail) = some (as,tail) := by
  induction as generalizing base with
  | nil => rfl
  | cons a as ih =>
    simp only [repeatEncode, List.map_append, List.append_assoc, repeatDecode, List.length_cons,
      (g base).roundtrip]
    dsimp only [Bind.bind, Option.bind]
    rw [ih]
    rfl

end Grammar
end HDLean.Backend.Text
