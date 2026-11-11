import HDLean.Backend.TextCodec
import HDLean.Backend.Graph

set_option backward.isDefEq.respectTransparency false
namespace HDLean.Backend.Proved
open Text

private def aagWord : Word := ⟨"aag", by decide, by decide⟩
private def single (n : Nat) : Line := ⟨[numeral n], by simp⟩
private def triple (a b c : Nat) : Line := ⟨[numeral a, numeral b, numeral c], by simp⟩

structure Aag where
  inputs : Nat
  next : List Nat
  outputs : List Nat
  gates : List Gate
  deriving Repr

structure Header where
  inputs : Nat
  latches : Nat
  outputs : Nat
  gates : Nat

private def headerLine (h : Header) : Line :=
  ⟨[aagWord, numeral (h.inputs+h.latches+h.gates), numeral h.inputs,
    numeral h.latches, numeral h.outputs, numeral h.gates], by simp⟩

private def readHeader : List String → Option Header
  | ["aag",m,i,l,o,a] => do
      let m ← m.toNat?; let i ← i.toNat?; let l ← l.toNat?
      let o ← o.toNat?; let a ← a.toNat?
      if m = i+l+a then some ⟨i,l,o,a⟩ else none
  | _ => none

private def inputLine (base : Nat) (_ : Unit) : Line := single (2*base)
private def latchLine (base n : Nat) : Line := triple (2*base) n (2*base)
private def outputLine (_ : Nat) (n : Nat) : Line := single n
private def gateLine (_ : Nat) (g : Gate) : Line := triple (2*g.lhs) g.a g.b

private def readInput (base : Nat) : List String → Option Unit
  | [n] => do
      let n ← n.toNat?
      if n = 2*base then some () else none
  | _ => none
private def readLatch (base : Nat) : List String → Option Nat
  | [q,n,init] => do
      let q ← q.toNat?; let n ← n.toNat?; let init ← init.toNat?
      if q = 2*base ∧ init = q then some n else none
  | _ => none
private def readOutput (_ : Nat) : List String → Option Nat
  | [n] => n.toNat?
  | _ => none
private def readGate (_ : Nat) : List String → Option Gate
  | [lhs,a,b] => do
      let lhs ← lhs.toNat?; let a ← a.toNat?; let b ← b.toNat?
      if lhs % 2 = 0 then some ⟨lhs/2,a,b⟩ else none
  | _ => none

@[simp] private theorem read_header (h : Header) : readHeader (headerLine h).tokens = some h := by
  cases h
  simp [readHeader, headerLine, Line.tokens, aagWord]
@[simp] private theorem read_input (k : Nat) (u : Unit) : readInput k (inputLine k u).tokens = some u := by
  cases u
  simp [readInput, inputLine, single, Line.tokens]
@[simp] private theorem read_latch (k n : Nat) : readLatch k (latchLine k n).tokens = some n := by
  simp [readLatch, latchLine, triple, Line.tokens]
@[simp] private theorem read_output (k n : Nat) : readOutput k (outputLine k n).tokens = some n := by
  simp [readOutput, outputLine, single, Line.tokens]
@[simp] private theorem read_gate (k : Nat) (g : Gate) : readGate k (gateLine k g).tokens = some g := by
  cases g
  simp [readGate, gateLine, triple, Line.tokens]

private def indexedLines (line : Nat → α → Line) : Nat → List α → List Line
  | _, [] => []
  | base, a::as => line base a :: indexedLines line (base+1) as

private def many (read : Nat → List String → Option α) :
    Nat → Nat → List (List String) → Option (List α × List (List String))
  | 0, _, rest => some ([],rest)
  | _+1, _, [] => none
  | n+1, base, line::rest => do
      let a ← read base line
      let (as,rest) ← many read n (base+1) rest
      return (a::as,rest)

private theorem many_roundtrip (read : Nat → List String → Option α) (line : Nat → α → Line)
    (h : ∀ k a, read k (line k a).tokens = some a)
    (as : List α) (base : Nat) (tail : List (List String)) :
    many read as.length base ((indexedLines line base as).map Line.tokens ++ tail) = some (as,tail) := by
  induction as generalizing base with
  | nil => rfl
  | cons a as ih => simp [many, indexedLines, h, ih]

/-- Canonical ASCII AIGER, built by linear list traversal and ending in a newline. -/
def Aag.lines (g : Aag) : List Line :=
  headerLine ⟨g.inputs,g.next.length,g.outputs.length,g.gates.length⟩ ::
    indexedLines inputLine 1 (List.replicate g.inputs ()) ++
    indexedLines latchLine (g.inputs+1) g.next ++
    indexedLines outputLine 0 g.outputs ++ indexedLines gateLine 0 g.gates

def Aag.print (g : Aag) : String := Text.render g.lines

private def parseAagTokens : List (List String) → Option Aag
  | [] => none
  | line::rest => do
      let h ← readHeader line
      let (_,rest) ← many readInput h.inputs 1 rest
      let (ns,rest) ← many readLatch h.latches (h.inputs+1) rest
      let (os,rest) ← many readOutput h.outputs 0 rest
      let (gs,rest) ← many readGate h.gates 0 rest
      if rest.isEmpty then some ⟨h.inputs,ns,os,gs⟩ else none

def Aag.parse (text : String) : Option Aag := parseAagTokens (Text.tokenize text)

/-- The real text decoder reconstructs every field; no semantic validator runs. -/
theorem Aag.parse_print (g : Aag) : Aag.parse g.print = some g := by
  unfold Aag.parse Aag.print
  rw [Text.tokenize_render]
  simp only [Aag.lines, List.map_cons, List.map_append, parseAagTokens, List.cons_append, read_header]
  dsimp only [Bind.bind, Option.bind]
  simp only [List.append_assoc]
  have hi := many_roundtrip readInput inputLine read_input (List.replicate g.inputs ()) 1
  simp only [List.length_replicate] at hi
  rw [hi]
  dsimp only [Bind.bind, Option.bind]
  rw [many_roundtrip readLatch latchLine read_latch]
  dsimp only [Bind.bind, Option.bind]
  rw [many_roundtrip readOutput outputLine read_output]
  dsimp only [Bind.bind, Option.bind]
  have h := many_roundtrip readGate gateLine read_gate g.gates 0 []
  simp only [List.append_nil] at h
  rw [h]
  cases g
  rfl

/-- Metadata has no cycle semantics and is restored in a canonical form. -/
def Aag.graph (g : Aag) : Graph :=
  ⟨"hd_top",g.inputs,g.next.length,g.gates.toArray,g.next.toArray,g.outputs.toArray,
    Array.replicate g.inputs "",Array.replicate g.next.length "",
    Array.replicate g.outputs.length "",Array.replicate g.next.length ""⟩

def Aag.ofGraph (g : Graph) : Aag := ⟨g.inputs,g.next.toList,g.outputs.toList,g.gates.toList⟩

@[simp] theorem Aag.ofGraph_logic (g : Graph) (hn : g.next.size = g.latches) : (Aag.ofGraph g).graph.logic = g.logic := by
  cases g
  simp_all [Aag.ofGraph, Aag.graph, Graph.logic]

/-- Instantiating this theorem requires no concrete parsing or graph reduction. -/
theorem aiger_print_correct (g : Graph) (hn : g.next.size = g.latches) (inputs state : Array Bool) :
    (Aag.parse (Aag.ofGraph g).print).map (fun parsed => parsed.graph.eval inputs state) = some (g.eval inputs state) := by
  rw [Aag.parse_print]
  simp only [Option.map_some]
  exact congrArg some (Graph.eval_eq_of_logic_eq _ _ (Aag.ofGraph_logic g hn) inputs state)

end HDLean.Backend.Proved
