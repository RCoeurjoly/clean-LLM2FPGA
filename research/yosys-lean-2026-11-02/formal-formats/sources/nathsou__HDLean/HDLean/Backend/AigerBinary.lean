import HDLean.Backend.Graph

/-!
# Binary AIGER, for talking to external optimisers

ABC reads and writes binary AIGER only. These two functions are not proved and
need not be: what goes out is a copy of a graph the proved emitter already
covers, and what comes back is checked by `Validate.select` before use.
-/
namespace HDLean.Backend.AigerBinary

partial def pushVarint (b : ByteArray) (x : Nat) : ByteArray :=
  if x < 128 then b.push x.toUInt8 else pushVarint (b.push (x % 128 + 128).toUInt8) (x / 128)

def pushLine (b : ByteArray) (s : String) : ByteArray := b ++ (s ++ "\n").toUTF8

/-- Binary AIGER of a well-formed graph (latches uninitialised). -/
def write (g : Graph) : ByteArray := Id.run do
  let n0 := g.inputs + g.latches + 1
  let mut b := pushLine ByteArray.empty
    s!"aig {g.inputs + g.latches + g.gates.size} {g.inputs} {g.latches} {g.outputs.size} {g.gates.size}"
  for l in g.next do b := pushLine b s!"{l}"
  for l in g.outputs do b := pushLine b s!"{l}"
  let mut k := 0
  for gt in g.gates do
    let lhs := 2 * (n0 + k)
    let r0 := max gt.a gt.b
    let r1 := min gt.a gt.b
    b := pushVarint (pushVarint b (lhs - r0)) (r0 - r1)
    k := k + 1
  return b

structure Parsed where
  inputs : Nat
  latches : Nat
  gates : Array Gate
  next : Array Lit
  outputs : Array Lit

/-- Read a line of ASCII starting at `pos`; returns the line and the next position. -/
def line (b : ByteArray) (pos : Nat) : String × Nat := Id.run do
  let mut p := pos
  while p < b.size && b[p]! != 10 do p := p + 1
  return (String.fromUTF8! (b.extract pos p), p + 1)

def varint (b : ByteArray) (pos : Nat) : Option (Nat × Nat) := Id.run do
  let mut p := pos
  let mut x := 0
  let mut shift := 0
  while p < b.size do
    let c := b[p]!.toNat
    x := x + (c % 128) * 2 ^ shift
    p := p + 1
    if c < 128 then return some (x, p)
    shift := shift + 7
  return none

def nums (s : String) : List Nat := (s.splitOn " ").filterMap (·.trimAscii.toString.toNat?)

/-- Binary AIGER (AIGER 1.0 sections only; latch initial values and symbols ignored). -/
def read (b : ByteArray) : Option Parsed := do
  let (hdr, p) := line b 0
  let toks := hdr.splitOn " "
  guard (toks.head? == some "aig")
  let ns := nums hdr
  let i ← ns[1]?
  let l ← ns[2]?
  let o ← ns[3]?
  let a ← ns[4]?
  let mut pos := p
  let mut next := #[]
  for _ in [:l] do
    let (s, q) := line b pos
    next := next.push (← (nums s).head?)
    pos := q
  let mut outs := #[]
  for _ in [:o] do
    let (s, q) := line b pos
    outs := outs.push (← (nums s).head?)
    pos := q
  let mut gates := #[]
  for k in [:a] do
    let lhs := 2 * (i + l + 1 + k)
    let (d0, q) ← varint b pos
    let (d1, q') ← varint b q
    pos := q'
    guard (d0 ≤ lhs)
    let r0 := lhs - d0
    guard (d1 ≤ r0)
    gates := gates.push ⟨i + l + 1 + k, r0, r0 - d1⟩
  return ⟨i, l, gates, next, outs⟩

def Parsed.graph (p : Parsed) (g : Graph) : Graph :=
  { g with gates := p.gates, next := p.next, outputs := p.outputs }

end HDLean.Backend.AigerBinary
