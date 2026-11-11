import HDLean.Rtl.RTLILStmt
import HDLean.Rtl.SystemVerilog

/-!
# Word-level RTLIL

One RTLIL module per design module, with Yosys's word-level cells: `$add`,
`$shl`, `$lt`, `$mux`, `$pos`, ... with explicit widths and signedness,
`$memrd`/`$memwr` on RTLIL memories, `$dff` registers (a `$mux` on `\rst` in
front for synchronous reset), and instances as cells of the child module.
Division and remainder spell out the zero-divisor case (`$logic_not` and
`$mux`) so that they agree with `BitVec`; NAND and NOR are `$not` after
`$and`/`$or`.

`Design.parse_rtlil`: parsing the printed text gives the design back.
`Design.parseRTLIL_canonical`: only the exact printing of a design parses.
-/
namespace HDLean.Rtl.IL
open Emit Lex SV

/-! ## Constants as bit concatenations -/

def bitTW (b : Bool) : TW := if b then lit! "1'1" else lit! "1'0"

/-- Bits `w-1 .. 0` of `v`, most significant first. -/
def bitsMSB : Nat → Nat → List TW
  | 0, _ => []
  | w + 1, v => bitTW (v.testBit w) :: bitsMSB w v

def constSig (w v : Nat) : List TW := lit! "{" :: bitsMSB w v ++ [lit! "}"]

def bitsVal : List Tok → Nat → Option Nat
  | [.lit "}"], acc => some acc
  | .lit "1'1" :: rest, acc => bitsVal rest (2 * acc + 1)
  | .lit "1'0" :: rest, acc => bitsVal rest (2 * acc)
  | _, _ => none

def decConst : List Tok → Option Nat
  | .lit "{" :: rest => bitsVal rest 0
  | _ => none

theorem mod_two_pow_succ (v w : Nat) : v % 2 ^ (w + 1) = (if v.testBit w then 2 ^ w else 0) + v % 2 ^ w := by
  rw [Nat.mod_pow_succ, Nat.testBit_eq_decide_div_mod_eq]
  rcases Nat.mod_two_eq_zero_or_one (v / 2 ^ w) with h | h <;> simp [h] <;> omega

theorem shift_arith (acc x r b : Nat) : (2 * acc + b) * x + r = acc * (x * 2) + (b * x + r) := by
  rw [Nat.add_mul, Nat.mul_assoc, Nat.mul_comm x 2, Nat.mul_left_comm acc 2 x]; omega

theorem bitsVal_bits (v : Nat) : ∀ (w acc : Nat),
    bitsVal (toks (bitsMSB w v) ++ [.lit "}"]) acc = some (acc * 2 ^ w + v % 2 ^ w)
  | 0, acc => by simp [bitsMSB, toks, bitsVal, Nat.mod_one]
  | w + 1, acc => by
    have ih := bitsVal_bits v w
    simp only [toks] at ih
    rw [mod_two_pow_succ, Nat.pow_succ]
    cases hb : v.testBit w
    · have e : toks (bitsMSB (w + 1) v) ++ [Tok.lit "}"] = .lit "1'0" :: (toks (bitsMSB w v) ++ [.lit "}"]) := by
        simp [bitsMSB, toks, hb, bitTW]
      rw [e, bitsVal]
      simp only [toks] at ih ⊢
      rw [ih]
      have := shift_arith acc (2 ^ w) (v % 2 ^ w) 0
      simp only [Nat.add_zero, Nat.zero_mul, Nat.zero_add, Bool.false_eq_true, ite_false] at this ⊢
      rw [this]
    · have e : toks (bitsMSB (w + 1) v) ++ [Tok.lit "}"] = .lit "1'1" :: (toks (bitsMSB w v) ++ [.lit "}"]) := by
        simp [bitsMSB, toks, hb, bitTW]
      rw [e, bitsVal]
      simp only [toks] at ih ⊢
      rw [ih]
      have := shift_arith acc (2 ^ w) (v % 2 ^ w) 1
      simp only [Nat.one_mul, ite_true] at this ⊢
      rw [this]

theorem decConst_sig (w v : Nat) (hv : v < 2 ^ w) : decConst (toks (constSig w v)) = some v := by
  have h := bitsVal_bits v w 0
  have e : toks (constSig w v) = .lit "{" :: (toks (bitsMSB w v) ++ [.lit "}"]) := by
    simp [constSig, toks]
  rw [e, decConst, h]
  simp [Nat.mod_eq_of_lt hv]

/-! ## Cells -/

def unary (ty : TW) (signed : Bool) (aw yw : Nat) (a y name : TW) : PStmt :=
  .cell ty name [(lit! "\\A_SIGNED", .num (if signed then 1 else 0)), (lit! "\\A_WIDTH", .num aw),
      (lit! "\\Y_WIDTH", .num yw)]
    [(lit! "\\A", [a]), (lit! "\\Y", [y])]

def binary (ty : TW) (sa sb : Bool) (aw bw yw : Nat) (a b y name : TW) : PStmt :=
  .cell ty name [(lit! "\\A_SIGNED", .num (if sa then 1 else 0)), (lit! "\\A_WIDTH", .num aw),
      (lit! "\\B_SIGNED", .num (if sb then 1 else 0)), (lit! "\\B_WIDTH", .num bw), (lit! "\\Y_WIDTH", .num yw)]
    [(lit! "\\A", [a]), (lit! "\\B", [b]), (lit! "\\Y", [y])]

def tA : Ident := ⟨"a", by decide⟩
def tC : Ident := ⟨"c", by decide⟩
def tD : Ident := ⟨"d", by decide⟩
def tQ : Ident := ⟨"q", by decide⟩
def tZ : Ident := ⟨"z", by decide⟩
def tFF : Ident := ⟨"ff", by decide⟩
def tRS : Ident := ⟨"rs", by decide⟩
def tRM : Ident := ⟨"rm", by decide⟩

/-- The `$mux` of a division or remainder: the quotient unless the divisor is zero. -/
def divMux (w k : Nat) (zero : List TW) : PStmt :=
  .cell (lit! "$mux") (W.inst k) [(lit! "\\WIDTH", .num w)]
    [(lit! "\\A", [W.temp k tQ]), (lit! "\\B", zero), (lit! "\\S", [W.temp k tZ]), (lit! "\\Y", [W.wire k])]

def divStmts (ty : TW) (w k a b : Nat) (zero : List TW) : List PStmt :=
  [.wire w .none (W.temp k tQ), .wire 1 .none (W.temp k tZ),
   binary ty false false w w w (W.wire a) (W.wire b) (W.temp k tQ) (W.temp k tC),
   unary (lit! "$logic_not") false w 1 (W.wire b) (W.temp k tZ) (W.temp k tD),
   divMux w k zero]

def notStmts (ty : TW) (w k a b : Nat) : List PStmt :=
  [.wire w .none (W.temp k tA), binary ty false false w w w (W.wire a) (W.wire b) (W.temp k tA) (W.temp k tC),
   unary (lit! "$not") false w w (W.temp k tA) (W.wire k) (W.inst k)]

def binStmts (m : Module) (w k : Nat) (op : BinOp) (a b : Nat) : List PStmt :=
  let simple (ty : TW) := [binary ty false false w w w (W.wire a) (W.wire b) (W.wire k) (W.inst k)]
  match op with
  | .and => simple (lit! "$and")
  | .or => simple (lit! "$or")
  | .xor => simple (lit! "$xor")
  | .xnor => simple (lit! "$xnor")
  | .add => simple (lit! "$add")
  | .sub => simple (lit! "$sub")
  | .mul => simple (lit! "$mul")
  | .shl => [binary (lit! "$shl") false false w (m.cellWidth b) w (W.wire a) (W.wire b) (W.wire k) (W.inst k)]
  | .lshr => [binary (lit! "$shr") false false w (m.cellWidth b) w (W.wire a) (W.wire b) (W.wire k) (W.inst k)]
  | .ashr => [binary (lit! "$sshr") true false w (m.cellWidth b) w (W.wire a) (W.wire b) (W.wire k) (W.inst k)]
  | .nand => notStmts (lit! "$and") w k a b
  | .nor => notStmts (lit! "$or") w k a b
  | .udiv => divStmts (lit! "$div") w k a b (constSig w 0)
  | .umod => divStmts (lit! "$mod") w k a b [W.wire a]

def cmpTy : CmpOp → TW
  | .eq => lit! "$eq" | .ne => lit! "$ne" | .ult => lit! "$lt" | .ule => lit! "$le"
  | .slt => lit! "$lt" | .sle => lit! "$le"

def cmpSigned : CmpOp → Bool
  | .slt | .sle => true
  | _ => false

def memAw (m : Module) (k : Nat) : Nat := (m.mems[k]?.map Mem.addrWidth).getD 0

def outConns (d : Design) (j k : Nat) : Nat → Nat → List Nat → List (TW × List TW)
  | _, _, [] => []
  | i, lo, w :: ws =>
    (W.output i (subOutName d j i), [W.wire k, lit! "[", .num (lo + w - 1), lit! ":", .num lo, lit! "]"]) ::
      outConns d j k (i + 1) (lo + w) ws

def instConns (d : Design) (j k : Nat) (args outs : List Nat) : List (TW × List TW) :=
  [(lit! "\\clk", [lit! "\\clk"]), (lit! "\\rst", [lit! "\\rst"])] ++
  args.zipIdx.map (fun p => (W.input p.2 (subInName d j p.2), [W.wire p.1])) ++ outConns d j k 0 0 outs

def cellBody (d : Design) (m : Module) (k w : Nat) : Op → List PStmt
  | .const v => [.connect (W.wire k) (constSig w v)]
  | .input i => [.connect (W.wire k) [W.input i (inName m i)]]
  | .reg r => [.connect (W.wire k) [W.reg r (regName m r)]]
  | .memRead mi a =>
    [.attr (lit! "\\hdlean_mem") (.num mi),
     .cell (lit! "$memrd") (W.inst k)
      [(lit! "\\MEMID", W.memId mi (memName m mi)), (lit! "\\ABITS", .num (memAw m mi)), (lit! "\\WIDTH", .num w),
       (lit! "\\CLK_ENABLE", .num 0), (lit! "\\CLK_POLARITY", .num 0), (lit! "\\TRANSPARENT", .num 0)]
      [(lit! "\\CLK", [lit! "1'0"]), (lit! "\\EN", [lit! "1'1"]), (lit! "\\ADDR", [W.wire a]),
       (lit! "\\DATA", [W.wire k])]]
  | .not a => [unary (lit! "$not") false w w (W.wire a) (W.wire k) (W.inst k)]
  | .neg a => [unary (lit! "$neg") false w w (W.wire a) (W.wire k) (W.inst k)]
  | .bin op a b => binStmts m w k op a b
  | .cmp op cw a b =>
    [binary (cmpTy op) (cmpSigned op) (cmpSigned op) cw cw w (W.wire a) (W.wire b) (W.wire k) (W.inst k)]
  | .mux s a b =>
    [.cell (lit! "$mux") (W.inst k) [(lit! "\\WIDTH", .num w)]
      [(lit! "\\A", [W.wire b]), (lit! "\\B", [W.wire a]), (lit! "\\S", [W.wire s]), (lit! "\\Y", [W.wire k])]]
  | .slice a lo => [.connect (W.wire k) [W.wire a, lit! "[", .num (lo + w - 1), lit! ":", .num lo, lit! "]"]]
  | .concat h l lw =>
    [.connect (W.wire k) [lit! "{", W.wire h, W.wire l, lit! "[", .num (lw - 1), lit! ":", .num 0, lit! "]", lit! "}"]]
  | .zext a => [unary (lit! "$pos") false (m.cellWidth a) w (W.wire a) (W.wire k) (W.inst k)]
  | .sext a fw => [unary (lit! "$pos") true fw w (W.wire a) (W.wire k) (W.inst k)]
  | .inst j args outs => [.cell (W.module j (subName d j)) (W.inst k) [] (instConns d j k args outs)]

def cellStmts (d : Design) (m : Module) (k : Nat) (c : Cell) : List PStmt :=
  .wire c.width .none (W.wire k) :: cellBody d m k c.width c.op

/-! ## Registers, memories, modules -/

def regStmts (k : Nat) (r : Reg) : List PStmt :=
  let ff (dIn : TW) : PStmt := .cell (lit! "$dff") (W.temp k tFF)
    [(lit! "\\CLK_POLARITY", .num 1), (lit! "\\WIDTH", .num r.width)]
    [(lit! "\\CLK", [lit! "\\clk"]), (lit! "\\D", [dIn]), (lit! "\\Q", [W.reg k r.name])]
  match r.reset with
  | .none => [ff (W.wire r.next)]
  | .sync v =>
    [.wire r.width .none (W.temp k tRS),
     .cell (lit! "$mux") (W.temp k tRM) [(lit! "\\WIDTH", .num r.width)]
      [(lit! "\\A", [W.wire r.next]), (lit! "\\B", constSig r.width v), (lit! "\\S", [lit! "\\rst"]),
       (lit! "\\Y", [W.temp k tRS])],
     ff (W.temp k tRS)]

/-- A write port's cell name, `\wr_<memory>_<port>`. -/
def wrWord (k j : Nat) : Backend.Text.Word :=
  ⟨"\\wr_" ++ Nat.repr k ++ "_" ++ Nat.repr j,
    by simp [repr_not_mem k ' ' (by decide), repr_not_mem j ' ' (by decide)],
    by simp [repr_not_mem k '\n' (by decide), repr_not_mem j '\n' (by decide)]⟩

theorem classify_wr (k j : Nat) : classify (wrWord k j).text = .lit (wrWord k j).text := by
  have key : ∀ L : List Char, classifyList ('\\' :: 'w' :: 'r' :: '_' :: L) =
      .lit (String.ofList ('\\' :: 'w' :: 'r' :: '_' :: L)) := fun L => rfl
  have h : (wrWord k j).text.toList = '\\' :: 'w' :: 'r' :: '_' :: ((Nat.repr k).toList ++ '_' :: (Nat.repr j).toList) := by
    simp [wrWord, String.toList_append]
  unfold classify
  rw [h, key, ← h, String.ofList_toList]

def W.wr (k j : Nat) : TW := ⟨wrWord k j, .lit (wrWord k j).text, classify_wr k j⟩

def enSig (w en : Nat) : List TW := lit! "{" :: (List.replicate w (W.wire en)) ++ [lit! "}"]

def writeStmt (k : Nat) (mm : Mem) (j : Nat) (p : WritePort) : PStmt :=
  .cell (lit! "$memwr") (W.wr k j)
    [(lit! "\\MEMID", W.memId k mm.name), (lit! "\\ABITS", .num mm.addrWidth), (lit! "\\WIDTH", .num mm.width),
     (lit! "\\CLK_ENABLE", .num 1), (lit! "\\CLK_POLARITY", .num 1), (lit! "\\PRIORITY", .num j)]
    [(lit! "\\CLK", [lit! "\\clk"]), (lit! "\\EN", enSig mm.width p.en), (lit! "\\ADDR", [W.wire p.addr]),
     (lit! "\\DATA", [W.wire p.data])]

def memStmts (k : Nat) (mm : Mem) : List PStmt :=
  [.comment [lit! "memory", .num mm.addrWidth], .memory mm.width (2 ^ mm.addrWidth) (W.mem k mm.name)]

def writeStmts (k : Nat) (mm : Mem) : List PStmt :=
  .comment [lit! "writes", .num mm.writes.size] :: mm.writes.toList.zipIdx.map fun q => writeStmt k mm q.2 q.1

def moduleStmts (d : Design) (k : Nat) (m : Module) : List PStmt :=
  [.comment [lit! "hdlean", lit! "module", .num m.inputs.size, .num m.outputs.size, .num m.cells.size,
      .num m.regs.size, .num m.mems.size],
   .module (W.module k m.name),
   .wire 1 (.input 1) (lit! "\\clk"), .wire 1 (.input 2) (lit! "\\rst")] ++
  m.inputs.toList.zipIdx.map (fun p => .wire p.1.width (.input (p.2 + 3)) (W.input p.2 p.1.name)) ++
  m.outputs.toList.zipIdx.map (fun p =>
    .wire (m.cellWidth p.1.node) (.output (m.inputs.size + 3 + p.2)) (W.output p.2 p.1.name)) ++
  m.regs.toList.zipIdx.map (fun p => .wire p.1.width .none (W.reg p.2 p.1.name)) ++
  m.mems.toList.zipIdx.flatMap (fun p => memStmts p.2 p.1) ++
  m.cells.toList.zipIdx.flatMap (fun p => cellStmts d m p.2 p.1) ++
  m.regs.toList.zipIdx.flatMap (fun p => regStmts p.2 p.1) ++
  m.mems.toList.zipIdx.flatMap (fun p => writeStmts p.2 p.1) ++
  m.outputs.toList.zipIdx.map (fun p => .connect (W.output p.2 p.1.name) [W.wire p.1.node]) ++
  [.endm]

def designStmts (d : Design) : List PStmt :=
  .comment [lit! "hdlean", lit! "design", .num d.modules.size] ::
  d.modules.toList.zipIdx.flatMap (fun p => moduleStmts d p.2 p.1)

def _root_.HDLean.Rtl.Design.rtlil (d : Design) : String := render (designStmts d)

/-! ## Decoding -/

def get (key : String) : List (Tok × α) → Option α
  | [] => none
  | (k, v) :: rest => if k = .lit key then some v else get key rest

def sigWire : List Tok → Option Nat
  | [.wire a] => some a
  | _ => none

def numOf : Tok → Option Nat
  | .num n => some n
  | _ => none

def argOf : Tok × List Tok → Option Nat
  | (.named 'i' _ _, [.wire a]) => some a
  | _ => none

def outOf : Tok × List Tok → Option Nat
  | (.named 'o' _ _, [.wire _, .lit "[", .num hi, .lit ":", .num lo, .lit "]"]) => some (hi + 1 - lo)
  | _ => none

def ofConnect (ts : List Tok) : Option Op :=
  match decConst ts with
  | some v => some (.const v)
  | none =>
    match ts with
    | [.named 'i' i _] => some (.input i)
    | [.named 'r' r _] => some (.reg r)
    | [.wire a, .lit "[", .num _, .lit ":", .num lo, .lit "]"] => some (.slice a lo)
    | [.lit "{", .wire h, .wire l, .lit "[", .num hi, .lit ":", .num 0, .lit "]", .lit "}"] => some (.concat h l (hi + 1))
    | _ => none

def ofCell (ty : Tok) (ps : List (Tok × Tok)) (cs : List (Tok × List Tok)) : Option Op :=
  let a := (get "\\A" cs).bind sigWire
  let b := (get "\\B" cs).bind sigWire
  let bin (o : BinOp) : Option Op := do pure (.bin o (← a) (← b))
  let cmp (o : CmpOp) : Option Op := do pure (.cmp o (← (get "\\A_WIDTH" ps).bind numOf) (← a) (← b))
  let signed := get "\\A_SIGNED" ps == some (.num 1)
  match ty with
  | .lit "$not" => a.map .not
  | .lit "$neg" => a.map .neg
  | .lit "$and" => bin .and
  | .lit "$or" => bin .or
  | .lit "$xor" => bin .xor
  | .lit "$xnor" => bin .xnor
  | .lit "$add" => bin .add
  | .lit "$sub" => bin .sub
  | .lit "$mul" => bin .mul
  | .lit "$shl" => bin .shl
  | .lit "$shr" => bin .lshr
  | .lit "$sshr" => bin .ashr
  | .lit "$eq" => cmp .eq
  | .lit "$ne" => cmp .ne
  | .lit "$lt" => cmp (if signed then .slt else .ult)
  | .lit "$le" => cmp (if signed then .sle else .ule)
  | .lit "$mux" => do pure (.mux (← (get "\\S" cs).bind sigWire) (← b) (← a))
  | .lit "$pos" => if signed then do pure (.sext (← a) (← (get "\\A_WIDTH" ps).bind numOf)) else a.map .zext
  | .named 'h' j _ => some (.inst j (cs.filterMap argOf) (cs.filterMap outOf))
  | _ => none

def ab (cs : List (Tok × List Tok)) (f : Nat → Nat → Op) : Option Op := do
  pure (f (← (get "\\A" cs).bind sigWire) (← (get "\\B" cs).bind sigWire))

def decBody : List Stmt → Option (Op × List Stmt)
  | .connect _ rhs :: rest => (ofConnect rhs).map (·, rest)
  | .attr (.lit "\\hdlean_mem") (.num mi) :: .cell _ _ _ cs :: rest =>
    ((get "\\ADDR" cs).bind sigWire).map fun a => (.memRead mi a, rest)
  | .wire _ _ _ :: .cell (.lit "$and") _ _ cs :: .cell _ _ _ _ :: rest => (ab cs (.bin .nand)).map (·, rest)
  | .wire _ _ _ :: .cell (.lit "$or") _ _ cs :: .cell _ _ _ _ :: rest => (ab cs (.bin .nor)).map (·, rest)
  | .wire _ _ _ :: .wire _ _ _ :: .cell (.lit "$div") _ _ cs :: .cell _ _ _ _ :: .cell _ _ _ _ :: rest =>
    (ab cs (.bin .udiv)).map (·, rest)
  | .wire _ _ _ :: .wire _ _ _ :: .cell (.lit "$mod") _ _ cs :: .cell _ _ _ _ :: .cell _ _ _ _ :: rest =>
    (ab cs (.bin .umod)).map (·, rest)
  | .cell ty _ ps cs :: rest => (ofCell ty ps cs).map (·, rest)
  | _ => none

def decCell : List Stmt → Option (Cell × List Stmt)
  | .wire w .none (.wire _) :: rest => (decBody rest).map fun p => (⟨w, p.1⟩, p.2)
  | _ => none

def decReg : List Stmt → Option ((Reset × Nat) × List Stmt)
  | .cell (.lit "$dff") _ _ cs :: rest => ((get "\\D" cs).bind sigWire).map fun n => ((.none, n), rest)
  | .wire _ _ _ :: .cell _ _ _ cs :: .cell _ _ _ _ :: rest => do
    let n ← (get "\\A" cs).bind sigWire
    let v ← (get "\\B" cs).bind decConst
    pure ((.sync v, n), rest)
  | _ => none

def enOf : Option (List Tok) → Option Nat
  | some (.lit "{" :: .wire e :: _) => some e
  | _ => none

def decWrite : List Stmt → Option (WritePort × List Stmt)
  | .cell _ _ _ cs :: rest => do
    let en ← enOf (get "\\EN" cs)
    let a ← (get "\\ADDR" cs).bind sigWire
    let dt ← (get "\\DATA" cs).bind sigWire
    pure (⟨en, a, dt⟩, rest)
  | _ => none

abbrev SDec (α : Type) := List Stmt → Option (α × List Stmt)

def sOne (f : Stmt → Option α) : SDec α
  | s :: rest => (f s).map (·, rest)
  | [] => none

def sRepeat (d : Nat → SDec α) : Nat → Nat → SDec (List α)
  | 0, _, ss => some ([], ss)
  | n + 1, base, ss => do
    let (a, ss) ← d base ss
    let (as, ss) ← sRepeat d n (base + 1) ss
    pure (a :: as, ss)

def decMemDecl : SDec (Ident × Nat × Nat)
  | .comment [.lit "memory", .num aw] :: .memory w _ (.named 'm' _ n) :: rest => some ((n, w, aw), rest)
  | _ => none

def decWrites : SDec (List WritePort) := fun ss => do
  let (n, ss) ← sOne (fun | .comment [.lit "writes", .num n] => some n | _ => none) ss
  sRepeat (fun _ => decWrite) n 0 ss

def headerOf : Stmt → Option (Nat × Nat × Nat × Nat × Nat)
  | .comment [.lit "hdlean", .lit "module", .num ni, .num no, .num nc, .num nr, .num nm] => some (ni, no, nc, nr, nm)
  | _ => none

def moduleNameOf : Stmt → Option Ident
  | .module (.named 'h' _ n) => some n
  | _ => none

def inputOf : Stmt → Option Port
  | .wire w (.input _) (.named 'i' _ n) => some ⟨n, w⟩
  | _ => none

def outputNameOf : Stmt → Option Ident
  | .wire _ (.output _) (.named 'o' _ n) => some n
  | _ => none

def regDeclOf : Stmt → Option (Ident × Nat)
  | .wire w .none (.named 'r' _ n) => some (n, w)
  | _ => none

def outputNodeOf : Stmt → Option Nat
  | .connect (.named 'o' _ _) [.wire n] => some n
  | _ => none

def endOf : Stmt → Option Unit
  | .endm => some ()
  | _ => none

def decModule (_ : Nat) : SDec Module := fun ss => do
  let ((ni, no, nc, nr, nm), ss) ← sOne headerOf ss
  let (name, ss) ← sOne moduleNameOf ss
  let ((), ss) ← sOne (fun _ => some ()) ss
  let ((), ss) ← sOne (fun _ => some ()) ss
  let (ins, ss) ← sRepeat (fun _ => sOne inputOf) ni 0 ss
  let (onames, ss) ← sRepeat (fun _ => sOne outputNameOf) no 0 ss
  let (rdecls, ss) ← sRepeat (fun _ => sOne regDeclOf) nr 0 ss
  let (mdecls, ss) ← sRepeat (fun _ => decMemDecl) nm 0 ss
  let (cells, ss) ← sRepeat (fun _ => decCell) nc 0 ss
  let (resets, ss) ← sRepeat (fun _ => decReg) nr 0 ss
  let (writes, ss) ← sRepeat (fun _ => decWrites) nm 0 ss
  let (nodes, ss) ← sRepeat (fun _ => sOne outputNodeOf) no 0 ss
  let ((), ss) ← sOne endOf ss
  pure (⟨name, ins.toArray, (onames.zipWith (fun n o => (⟨n, o⟩ : OutPort)) nodes).toArray, cells.toArray,
    (rdecls.zipWith (fun d r => (⟨d.1, d.2, r.1, r.2⟩ : Reg)) resets).toArray,
    (mdecls.zipWith (fun d ws => (⟨d.1, d.2.2, d.2.1, ws.toArray⟩ : Mem)) writes).toArray⟩, ss)

def decDesign : SDec Design := fun ss => do
  let (n, ss) ← sOne (fun | .comment [.lit "hdlean", .lit "design", .num n] => some n | _ => none) ss
  let (ms, ss) ← sRepeat decModule n 0 ss
  pure (⟨ms.toArray⟩, ss)

/-- The meaning of an RTLIL file: the design it decodes to, accepted only if
printing that design reproduces the file exactly. -/
def _root_.HDLean.Rtl.Design.parseRTLIL (text : String) : Option Design :=
  match (lexStmts text).bind decDesign with
  | some (d, []) => if d.rtlil = text then some d else none
  | _ => none

end HDLean.Rtl.IL
