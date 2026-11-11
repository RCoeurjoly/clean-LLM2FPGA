import HDLean.Rtl.TextBase

/-!
# Word-level SystemVerilog

One `module` per design module, ports and wires named after the design
(`i_0_en`, `r_2_pc`, `n_17`), one continuous assignment per cell, one clocked
`always` per register, and one clocked block per memory with its write ports in
priority order (block-RAM inference template). Division and remainder spell out
the zero-divisor case so that they agree with `BitVec` (`x / 0 = 0`, `x % 0 = x`).
-/
namespace HDLean.Rtl.SV
open Emit Lex

/-- Every width that the text spells as `width - 1` is positive. -/
def _root_.HDLean.Rtl.Op.positive : Op → Bool
  | .cmp _ cw _ _ => 0 < cw
  | .concat _ _ lw => 0 < lw
  | .sext _ fw => 0 < fw
  | .inst _ _ outs => outs.all (0 < ·)
  | _ => true

def range (w : Nat) : List TW := [lit! "[", .num (w - 1), lit! ":", .num 0, lit! "]"]

def decRange : List Tok → Option (Nat × List Tok)
  | .lit "[" :: .num hi :: .lit ":" :: .num 0 :: .lit "]" :: rest => some (hi + 1, rest)
  | _ => none

@[simp] theorem range_toks (w : Nat) : (range w).map TW.tok =
    [.lit "[", .num (w - 1), .lit ":", .num 0, .lit "]"] := rfl

theorem decRange_ok (w : Nat) (hw : 0 < w) (rest : List Tok) :
    decRange ((range w).map TW.tok ++ rest) = some (w, rest) := by
  simp [range, decRange, TW.num, Nat.sub_add_cancel hw]

def binSym : BinOp → String
  | .and => "&" | .or => "|" | .xor => "^" | .add => "+" | .sub => "-" | .mul => "*"
  | .shl => "<<" | .lshr => ">>" | .nand => "&" | .nor => "|" | .xnor => "^"
  | .udiv => "/" | .umod => "%" | .ashr => ">>>"

def binTWs (w : Nat) : BinOp → Nat → Nat → List TW
  | .and, a, b => [.wire a, lit! "&", .wire b, lit! ";"]
  | .or, a, b => [.wire a, lit! "|", .wire b, lit! ";"]
  | .xor, a, b => [.wire a, lit! "^", .wire b, lit! ";"]
  | .add, a, b => [.wire a, lit! "+", .wire b, lit! ";"]
  | .sub, a, b => [.wire a, lit! "-", .wire b, lit! ";"]
  | .mul, a, b => [.wire a, lit! "*", .wire b, lit! ";"]
  | .shl, a, b => [.wire a, lit! "<<", .wire b, lit! ";"]
  | .lshr, a, b => [.wire a, lit! ">>", .wire b, lit! ";"]
  | .nand, a, b => [lit! "~", lit! "(", .wire a, lit! "&", .wire b, lit! ")", lit! ";"]
  | .nor, a, b => [lit! "~", lit! "(", .wire a, lit! "|", .wire b, lit! ")", lit! ";"]
  | .xnor, a, b => [lit! "~", lit! "(", .wire a, lit! "^", .wire b, lit! ")", lit! ";"]
  | .udiv, a, b => [lit! "(", .wire b, lit! "==", .const w 0, lit! ")", lit! "?", .const w 0, lit! ":",
      lit! "(", .wire a, lit! "/", .wire b, lit! ")", lit! ";"]
  | .umod, a, b => [lit! "(", .wire b, lit! "==", .const w 0, lit! ")", lit! "?", .wire a, lit! ":",
      lit! "(", .wire a, lit! "%", .wire b, lit! ")", lit! ";"]
  | .ashr, a, b => [lit! "$signed", lit! "(", .wire a, lit! ")", lit! ">>>", .wire b, lit! ";"]

def cmpTWs : CmpOp → Nat → Nat → Nat → List TW
  | .eq, cw, a, b => [.wire a] ++ range cw ++ [lit! "==", .wire b] ++ range cw ++ [lit! ";"]
  | .ne, cw, a, b => [.wire a] ++ range cw ++ [lit! "!=", .wire b] ++ range cw ++ [lit! ";"]
  | .ult, cw, a, b => [.wire a] ++ range cw ++ [lit! "<", .wire b] ++ range cw ++ [lit! ";"]
  | .ule, cw, a, b => [.wire a] ++ range cw ++ [lit! "<=", .wire b] ++ range cw ++ [lit! ";"]
  | .slt, cw, a, b => [lit! "$signed", lit! "(", .wire a] ++ range cw ++
      [lit! ")", lit! "<", lit! "$signed", lit! "(", .wire b] ++ range cw ++ [lit! ")", lit! ";"]
  | .sle, cw, a, b => [lit! "$signed", lit! "(", .wire a] ++ range cw ++
      [lit! ")", lit! "<=", lit! "$signed", lit! "(", .wire b] ++ range cw ++ [lit! ")", lit! ";"]

def inName (m : Module) (k : Nat) : Ident := (m.inputs[k]?.map Port.name).getD default
def regName (m : Module) (k : Nat) : Ident := (m.regs[k]?.map Reg.name).getD default
def memName (m : Module) (k : Nat) : Ident := (m.mems[k]?.map Mem.name).getD default

/-- The tokens after `wire [w-1:0] n_k =` of a non-instance cell. -/
def exprTWs (m : Module) (w : Nat) : Op → List TW
  | .const v => [.const w v, lit! ";"]
  | .input k => [.input k (inName m k), lit! ";"]
  | .reg k => [.reg k (regName m k), lit! ";"]
  | .memRead mi a => [.mem mi (memName m mi), lit! "[", .wire a, lit! "]", lit! ";"]
  | .not a => [lit! "~", .wire a, lit! ";"]
  | .neg a => [lit! "-", .wire a, lit! ";"]
  | .bin op a b => binTWs w op a b
  | .cmp op cw a b => cmpTWs op cw a b
  | .mux s a b => [lit! "(", .wire s, lit! ")", lit! "?", .wire a, lit! ":", .wire b, lit! ";"]
  | .slice a lo => [.wire a, lit! "[", .num (lo + w - 1), lit! ":", .num lo, lit! "]", lit! ";"]
  | .concat h l lw => [lit! "{", .wire h, lit! ",", .wire l] ++ range lw ++ [lit! "}", lit! ";"]
  | .zext a => [.wire a, lit! ";"]
  | .sext a fw => [lit! "$signed", lit! "(", .wire a] ++ range fw ++ [lit! ")", lit! ";"]
  | .inst .. => [lit! ";"]

def binOf : String → Option BinOp
  | "&" => some .and | "|" => some .or | "^" => some .xor | "+" => some .add | "-" => some .sub
  | "*" => some .mul | "<<" => some .shl | ">>" => some .lshr | _ => none

def notBinOf : String → Option BinOp
  | "&" => some .nand | "|" => some .nor | "^" => some .xnor | _ => none

def cmpOf : String → Option CmpOp
  | "==" => some .eq | "!=" => some .ne | "<" => some .ult | "<=" => some .ule | _ => none

def scmpOf : String → Option CmpOp
  | "<" => some .slt | "<=" => some .sle | _ => none

def decodeExpr : List Tok → Option Op
  | [.const _ v, .lit ";"] => some (.const v)
  | [.named 'i' k _, .lit ";"] => some (.input k)
  | [.named 'r' k _, .lit ";"] => some (.reg k)
  | [.named 'm' mi _, .lit "[", .wire a, .lit "]", .lit ";"] => some (.memRead mi a)
  | [.lit "~", .wire a, .lit ";"] => some (.not a)
  | [.lit "-", .wire a, .lit ";"] => some (.neg a)
  | [.wire a, .lit op, .wire b, .lit ";"] => (binOf op).map fun o => .bin o a b
  | [.lit "~", .lit "(", .wire a, .lit op, .wire b, .lit ")", .lit ";"] => (notBinOf op).map fun o => .bin o a b
  | [.lit "(", .wire b, .lit "==", .const _ _, .lit ")", .lit "?", .const _ _, .lit ":",
      .lit "(", .wire a, .lit "/", .wire _, .lit ")", .lit ";"] => some (.bin .udiv a b)
  | [.lit "(", .wire b, .lit "==", .const _ _, .lit ")", .lit "?", .wire a, .lit ":",
      .lit "(", .wire _, .lit "%", .wire _, .lit ")", .lit ";"] => some (.bin .umod a b)
  | [.lit "$signed", .lit "(", .wire a, .lit ")", .lit ">>>", .wire b, .lit ";"] => some (.bin .ashr a b)
  | [.lit "(", .wire s, .lit ")", .lit "?", .wire a, .lit ":", .wire b, .lit ";"] => some (.mux s a b)
  | [.wire a, .lit "[", .num _, .lit ":", .num lo, .lit "]", .lit ";"] => some (.slice a lo)
  | [.wire a, .lit ";"] => some (.zext a)
  | .lit "{" :: .wire h :: .lit "," :: .wire l :: rest =>
    match decRange rest with
    | some (lw, [.lit "}", .lit ";"]) => some (.concat h l lw)
    | _ => none
  | .lit "$signed" :: .lit "(" :: .wire a :: rest =>
    match decRange rest with
    | some (fw, [.lit ")", .lit ";"]) => some (.sext a fw)
    | some (cw, .lit ")" :: .lit op :: .lit "$signed" :: .lit "(" :: .wire b :: rest) =>
      match decRange rest with
      | some (_, [.lit ")", .lit ";"]) => (scmpOf op).map fun o => .cmp o cw a b
      | _ => none
    | _ => none
  | .wire a :: rest =>
    match decRange rest with
    | some (cw, .lit op :: .wire b :: rest) =>
      match decRange rest with
      | some (_, [.lit ";"]) => (cmpOf op).map fun o => .cmp o cw a b
      | _ => none
    | _ => none
  | _ => none

theorem decodeExpr_ok (m : Module) (w : Nat) (op : Op) (hp : op.positive = true)
    (hi : ∀ j args outs, op ≠ .inst j args outs) :
    decodeExpr ((exprTWs m w op).map TW.tok) = some op := by
  cases op with
  | bin o a b => cases o <;> rfl
  | cmp o cw a b =>
    have hcw : 0 < cw := by simpa [Op.positive] using hp
    cases o <;> simp [exprTWs, cmpTWs, decodeExpr, decRange, cmpOf, scmpOf, TW.wire, Nat.sub_add_cancel hcw]
  | concat h l lw =>
    have hlw : 0 < lw := by simpa [Op.positive] using hp
    simp [exprTWs, decodeExpr, decRange, TW.wire, Nat.sub_add_cancel hlw]
  | sext a fw =>
    have hfw : 0 < fw := by simpa [Op.positive] using hp
    simp [exprTWs, decodeExpr, decRange, TW.wire, Nat.sub_add_cancel hfw]
  | inst j args outs => exact absurd rfl (hi j args outs)
  | _ => rfl


/-! ## Modules -/

private theorem classify_dot (s : String) (c : Char) (rest : List Char) (hc : c = 'i' ∨ c = 'o')
    (h : s.toList = '.' :: c :: rest) : classify s = .lit s := by
  have hs : s = String.ofList ('.' :: c :: rest) := by rw [← h, String.ofList_toList]
  subst hs
  unfold classify
  rw [String.toList_ofList]
  rcases hc with rfl | rfl <;> rfl

/-- `.i_<k>_<name>` in a named port connection. -/
def dotTW (c : Char) (hc : c = 'i' ∨ c = 'o') (k : Nat) (n : Ident) : TW :=
  let base := namedWord (String.singleton c ++ "_") (by rcases hc with rfl | rfl <;> decide)
    (by rcases hc with rfl | rfl <;> decide) k n
  ⟨⟨"." ++ base.text, by simpa using base.noSpace, by simpa using base.noNewline⟩, .lit ("." ++ base.text),
    classify_dot _ c ('_' :: (Nat.toDigits 10 k ++ '_' :: n.text.toList)) hc
      (by simp [base, namedWord, String.toList_append])⟩

def subInName (d : Design) (j i : Nat) : Ident :=
  ((d.modules[j]?).bind (fun m => m.inputs[i]?) |>.map Port.name).getD default
def subOutName (d : Design) (j i : Nat) : Ident :=
  ((d.modules[j]?).bind (fun m => m.outputs[i]?) |>.map OutPort.name).getD default
def subName (d : Design) (j : Nat) : Ident := ((d.modules[j]?).map Module.name).getD default

def argLine (d : Design) (j : Nat) (p : Nat × Nat) : TLine :=
  ⟨lit! ",", [dotTW 'i' (.inl rfl) p.2 (subInName d j p.2), lit! "(", .wire p.1, lit! ")"]⟩

def outLines (d : Design) (j k : Nat) : Nat → Nat → List Nat → List TLine
  | _, _, [] => []
  | i, lo, w :: ws =>
    ⟨lit! ",", [dotTW 'o' (.inr rfl) i (subOutName d j i), lit! "(", .wire k, lit! "[", .num (lo + w - 1),
      lit! ":", .num lo, lit! "]", lit! ")"]⟩ :: outLines d j k (i + 1) (lo + w) ws

def cellLines (d : Design) (m : Module) (k : Nat) (c : Cell) : List TLine :=
  match c.op with
  | .inst j args outs =>
    [⟨lit! "wire", range c.width ++ [.wire k, lit! ";", lit! "//", lit! "instance", .num args.length, .num outs.length]⟩,
     ⟨.module j (subName d j), [.inst k, lit! "("]⟩,
     ⟨lit! ".clk", [lit! "(", lit! "clk", lit! ")", lit! ","]⟩,
     ⟨lit! ".rst", [lit! "(", lit! "rst", lit! ")"]⟩] ++
    args.zipIdx.flatMap (fun p => [argLine d j p]) ++ outLines d j k 0 0 outs ++ [⟨lit! ")", [lit! ";"]⟩]
  | op => [⟨lit! "wire", range c.width ++ [.wire k, lit! "="] ++ exprTWs m c.width op⟩]

def regDecl (k : Nat) (r : Reg) : TLine := ⟨lit! "reg", range r.width ++ [.reg k r.name, lit! ";"]⟩

def regAlways (k : Nat) (r : Reg) : TLine :=
  ⟨lit! "always", [lit! "@", lit! "(", lit! "posedge", lit! "clk", lit! ")", .reg k r.name, lit! "<="] ++
    (match r.reset with
     | .sync v => [lit! "rst", lit! "?", .const r.width v, lit! ":", .wire r.next, lit! ";"]
     | .none => [.wire r.next, lit! ";"])⟩

def writeLine (k : Nat) (name : Ident) (p : WritePort) : TLine :=
  ⟨lit! "if", [lit! "(", .wire p.en, lit! ")", .mem k name, lit! "[", .wire p.addr, lit! "]", lit! "<=",
    .wire p.data, lit! ";"]⟩

def memDecl (k : Nat) (mm : Mem) : TLine :=
  ⟨lit! "reg", range mm.width ++ [.mem k mm.name, lit! "[", .num 0, lit! ":", .num (2 ^ mm.addrWidth - 1), lit! "]",
     lit! ";", lit! "//", lit! "address", .num mm.addrWidth]⟩

def memBlock (k : Nat) (mm : Mem) : List TLine :=
  ⟨lit! "always", [lit! "@", lit! "(", lit! "posedge", lit! "clk", lit! ")", lit! "begin", lit! "//", lit! "writes",
    .num mm.writes.size]⟩ ::
  (mm.writes.toList.zipIdx.flatMap fun p => [writeLine k mm.name p.1]) ++ [⟨lit! "end", []⟩]

def inputLine (k : Nat) (p : Port) : TLine :=
  ⟨lit! ",", [lit! "input", lit! "wire"] ++ range p.width ++ [.input k p.name]⟩

def outputDecl (m : Module) (k : Nat) (o : OutPort) : TLine :=
  ⟨lit! ",", [lit! "output", lit! "wire"] ++ range (m.cellWidth o.node) ++ [.output k o.name]⟩

def assignLine (k : Nat) (o : OutPort) : TLine :=
  ⟨lit! "assign", [.output k o.name, lit! "=", .wire o.node, lit! ";"]⟩

def headerLine (m : Module) : TLine :=
  ⟨lit! "//", [lit! "hdlean", lit! "module", .num m.inputs.size, .num m.outputs.size, .num m.cells.size,
      .num m.regs.size, .num m.mems.size]⟩

def moduleLines (d : Design) (k : Nat) (m : Module) : List TLine :=
  [headerLine m, ⟨lit! "module", [.module k m.name, lit! "("]⟩,
   ⟨lit! "input", [lit! "wire", lit! "clk", lit! ","]⟩, ⟨lit! "input", [lit! "wire", lit! "rst"]⟩] ++
  m.inputs.toList.zipIdx.flatMap (fun p => [inputLine p.2 p.1]) ++
  m.outputs.toList.zipIdx.flatMap (fun p => [outputDecl m p.2 p.1]) ++
  [⟨lit! ")", [lit! ";"]⟩] ++
  m.regs.toList.zipIdx.flatMap (fun p => [regDecl p.2 p.1]) ++
  m.mems.toList.zipIdx.flatMap (fun p => [memDecl p.2 p.1]) ++
  m.cells.toList.zipIdx.flatMap (fun p => cellLines d m p.2 p.1) ++
  m.regs.toList.zipIdx.flatMap (fun p => [regAlways p.2 p.1]) ++
  m.mems.toList.zipIdx.flatMap (fun p => memBlock p.2 p.1) ++
  m.outputs.toList.zipIdx.flatMap (fun p => [assignLine p.2 p.1]) ++
  [⟨lit! "endmodule", []⟩]

def designLines (d : Design) : List TLine :=
  ⟨lit! "//", [lit! "hdlean", lit! "design", .num d.modules.size]⟩ ::
  d.modules.toList.zipIdx.flatMap (fun p => moduleLines d p.2 p.1)

def _root_.HDLean.Rtl.Design.systemVerilog (d : Design) : String := renderLines (designLines d)


/-! ## Decoding -/

def decInput : Dec Port := oneLine fun
  | [.lit ",", .lit "input", .lit "wire", .lit "[", .num hi, .lit ":", .num 0, .lit "]", .named 'i' _ name] =>
    some ⟨name, hi + 1⟩
  | _ => none

def skipLine : Dec Unit := oneLine fun _ => some ()

def decRegDecl : Dec (Ident × Nat) := oneLine fun
  | [.lit "reg", .lit "[", .num hi, .lit ":", .num 0, .lit "]", .named 'r' _ name, .lit ";"] => some (name, hi + 1)
  | _ => none

def decMemDecl : Dec (Ident × Nat × Nat) := oneLine fun
  | [.lit "reg", .lit "[", .num hi, .lit ":", .num 0, .lit "]", .named 'm' _ name, .lit "[", .num 0, .lit ":",
      .num _, .lit "]", .lit ";", .lit "//", .lit "address", .num aw] =>
    some (name, hi + 1, aw)
  | _ => none

def decRegAlways : Dec (Reset × Nat) := oneLine fun
  | [.lit "always", .lit "@", .lit "(", .lit "posedge", .lit "clk", .lit ")", .named 'r' _ _, .lit "<=",
      .lit "rst", .lit "?", .const _ v, .lit ":", .wire n, .lit ";"] => some (.sync v, n)
  | [.lit "always", .lit "@", .lit "(", .lit "posedge", .lit "clk", .lit ")", .named 'r' _ _, .lit "<=",
      .wire n, .lit ";"] => some (.none, n)
  | _ => none

def decWrite : Dec WritePort := oneLine fun
  | [.lit "if", .lit "(", .wire en, .lit ")", .named 'm' _ _, .lit "[", .wire a, .lit "]", .lit "<=",
      .wire dt, .lit ";"] => some ⟨en, a, dt⟩
  | _ => none

def decMemBlock : Dec (List WritePort) := fun ls => do
  let (np, ls) ← oneLine (fun
    | [.lit "always", .lit "@", .lit "(", .lit "posedge", .lit "clk", .lit ")", .lit "begin", .lit "//",
        .lit "writes", .num np] => some np
    | _ => none) ls
  let (ws, ls) ← decRepeat (fun _ => decWrite) np 0 ls
  let ((), ls) ← fixedLine [.lit "end"] ls
  pure (ws, ls)

def decAssign : Dec OutPort := oneLine fun
  | [.lit "assign", .named 'o' _ name, .lit "=", .wire n, .lit ";"] => some ⟨name, n⟩
  | _ => none

def decArg : Dec Nat := oneLine fun
  | [.lit ",", .lit _, .lit "(", .wire a, .lit ")"] => some a
  | _ => none

def decOut : Dec Nat := oneLine fun
  | [.lit ",", .lit _, .lit "(", .wire _, .lit "[", .num hi, .lit ":", .num lo, .lit "]", .lit ")"] => some (hi + 1 - lo)
  | _ => none

def decInst (w na no : Nat) : Dec Cell := fun ls => do
  let (j, ls) ← oneLine (fun
    | [.named 'h' j _, .inst _, .lit "("] => some j
    | _ => none) ls
  let ((), ls) ← fixedLine [.lit ".clk", .lit "(", .lit "clk", .lit ")", .lit ","] ls
  let ((), ls) ← fixedLine [.lit ".rst", .lit "(", .lit "rst", .lit ")"] ls
  let (args, ls) ← decRepeat (fun _ => decArg) na 0 ls
  let (outs, ls) ← decRepeat (fun _ => decOut) no 0 ls
  let ((), ls) ← fixedLine [.lit ")", .lit ";"] ls
  pure (⟨w, .inst j args outs⟩, ls)

def decCell : Dec Cell
  | (.lit "wire" :: .lit "[" :: .num hi :: .lit ":" :: .num 0 :: .lit "]" :: .wire _ :: .lit "=" :: e) :: rest =>
    (decodeExpr e).map fun op => (⟨hi + 1, op⟩, rest)
  | [.lit "wire", .lit "[", .num hi, .lit ":", .num 0, .lit "]", .wire _, .lit ";", .lit "//", .lit "instance",
      .num na, .num no] :: rest => decInst (hi + 1) na no rest
  | _ => none

def decModule (_ : Nat) : Dec Module := fun ls => do
  let ((ni, no, nc, nr, nm), ls) ← oneLine (fun
    | [.lit "//", .lit "hdlean", .lit "module", .num ni, .num no, .num nc, .num nr, .num nm] => some (ni, no, nc, nr, nm)
    | _ => none) ls
  let (name, ls) ← oneLine (fun
    | [.lit "module", .named 'h' _ name, .lit "("] => some name
    | _ => none) ls
  let ((), ls) ← fixedLine [.lit "input", .lit "wire", .lit "clk", .lit ","] ls
  let ((), ls) ← fixedLine [.lit "input", .lit "wire", .lit "rst"] ls
  let (inputs, ls) ← decRepeat (fun _ => decInput) ni 0 ls
  let (_, ls) ← decRepeat (fun _ => skipLine) no 0 ls
  let ((), ls) ← fixedLine [.lit ")", .lit ";"] ls
  let (rdecls, ls) ← decRepeat (fun _ => decRegDecl) nr 0 ls
  let (mdecls, ls) ← decRepeat (fun _ => decMemDecl) nm 0 ls
  let (cells, ls) ← decRepeat (fun _ => decCell) nc 0 ls
  let (ralways, ls) ← decRepeat (fun _ => decRegAlways) nr 0 ls
  let (mwrites, ls) ← decRepeat (fun _ => decMemBlock) nm 0 ls
  let (outs, ls) ← decRepeat (fun _ => decAssign) no 0 ls
  let ((), ls) ← fixedLine [.lit "endmodule"] ls
  pure (⟨name, inputs.toArray, outs.toArray, cells.toArray,
    (rdecls.zipWith (fun d a => (⟨d.1, d.2, a.1, a.2⟩ : Reg)) ralways).toArray,
    (mdecls.zipWith (fun d ws => (⟨d.1, d.2.2, d.2.1, ws.toArray⟩ : Mem)) mwrites).toArray⟩, ls)

def decDesign : Dec Design := fun ls => do
  let (n, ls) ← oneLine (fun
    | [.lit "//", .lit "hdlean", .lit "design", .num n] => some n
    | _ => none) ls
  let (ms, ls) ← decRepeat decModule n 0 ls
  pure (⟨ms.toArray⟩, ls)

/-- The meaning of a SystemVerilog file: the design it decodes to, accepted only
if printing that design reproduces the file exactly. -/
def _root_.HDLean.Rtl.Design.parseSystemVerilog (text : String) : Option Design :=
  match decDesign (lex text) with
  | some (d, []) => if d.systemVerilog = text then some d else none
  | _ => none


/-! ## Round trip -/

def _root_.HDLean.Rtl.Module.positive (m : Module) : Bool :=
  m.inputs.toList.all (fun p => 0 < p.width) &&
  m.cells.toList.all (fun c => 0 < c.width && c.op.positive) &&
  m.regs.toList.all (fun r => 0 < r.width) && m.mems.toList.all (fun mm => 0 < mm.width)

def _root_.HDLean.Rtl.Design.positive (d : Design) : Bool := d.modules.toList.all Module.positive

/-- Repeated single-line items. -/
theorem lines_ok {β α : Type} (dec : Dec α) (line : Nat → β → TLine) (g : β → α) (P : β → Prop)
    (h : ∀ i x tail, P x → dec ((line i x).toks :: tail) = some (g x, tail))
    (xs : List β) (hp : ∀ x ∈ xs, P x) (tail : List (List Tok)) :
    decRepeat (fun _ => dec) xs.length 0
      ((xs.zipIdx.flatMap fun p => [line p.2 p.1]).map TLine.toks ++ tail) = some (xs.map g, tail) := by
  have := decRepeat_ok (fun _ => dec) (fun i x => [(line i x).toks]) g P (fun i x tail hx => h i x tail hx) xs 0 tail hp
  simpa [List.map_flatMap] using this

theorem decInput_ok (k : Nat) (p : Port) (tail : List (List Tok)) (hp : 0 < p.width) :
    decInput ((inputLine k p).toks :: tail) = some (p, tail) := by
  simp [decInput, oneLine, inputLine, TLine.toks, TW.input, TW.num, range, Nat.sub_add_cancel hp]

theorem decRegDecl_ok (k : Nat) (r : Reg) (tail : List (List Tok)) (hp : 0 < r.width) :
    decRegDecl ((regDecl k r).toks :: tail) = some ((r.name, r.width), tail) := by
  simp [decRegDecl, oneLine, regDecl, TLine.toks, TW.reg, TW.num, range, Nat.sub_add_cancel hp]

theorem decMemDecl_ok (k : Nat) (mm : Mem) (tail : List (List Tok)) (hp : 0 < mm.width) :
    decMemDecl ((memDecl k mm).toks :: tail) = some ((mm.name, mm.width, mm.addrWidth), tail) := by
  simp [decMemDecl, oneLine, memDecl, TLine.toks, TW.mem, TW.num, range, Nat.sub_add_cancel hp]

theorem decRegAlways_ok (k : Nat) (r : Reg) (tail : List (List Tok)) :
    decRegAlways ((regAlways k r).toks :: tail) = some ((r.reset, r.next), tail) := by
  cases h : r.reset <;> simp [decRegAlways, oneLine, regAlways, TLine.toks, TW.reg, TW.const, TW.wire, h]

theorem decAssign_ok (k : Nat) (o : OutPort) (tail : List (List Tok)) :
    decAssign ((assignLine k o).toks :: tail) = some (o, tail) := by
  simp [decAssign, oneLine, assignLine, TLine.toks, TW.output, TW.wire]

theorem decWrite_ok (k : Nat) (name : Ident) (p : WritePort) (tail : List (List Tok)) :
    decWrite ((writeLine k name p).toks :: tail) = some (p, tail) := by
  simp [decWrite, oneLine, writeLine, TLine.toks, TW.mem, TW.wire]

theorem decMemBlock_ok (k : Nat) (mm : Mem) (tail : List (List Tok)) :
    decMemBlock ((memBlock k mm).map TLine.toks ++ tail) = some (mm.writes.toList, tail) := by
  have hw := lines_ok decWrite (fun _ p => writeLine k mm.name p) id (fun _ => True)
    (fun i x tail _ => decWrite_ok k mm.name x tail) mm.writes.toList (fun _ _ => trivial)
    ([TLine.toks ⟨lit! "end", []⟩] ++ tail)
  simp only [List.map_id] at hw
  simp only [memBlock, List.map_cons, List.map_append, List.cons_append, List.append_assoc, decMemBlock]
  simp only [oneLine, TLine.toks, TW.num, List.map_cons, List.map_nil, List.nil_append, Option.map_some,
    Option.bind_eq_bind, Option.bind_some]
  simp only [TLine.toks, List.map_nil, List.singleton_append, Array.length_toList] at hw
  rw [hw]
  simp [fixedLine, oneLine]


theorem decOut_ok (d : Design) (j k i lo w : Nat) (hw : 0 < w) (tail : List (List Tok)) :
    decOut ((TLine.toks ⟨lit! ",", [dotTW 'o' (.inr rfl) i (subOutName d j i), lit! "(", .wire k, lit! "[",
      .num (lo + w - 1), lit! ":", .num lo, lit! "]", lit! ")"]⟩) :: tail) = some (w, tail) := by
  simp only [decOut, oneLine, TLine.toks, TW.num, TW.wire, dotTW, List.map_cons, List.map_nil, Option.map_some]
  congr
  omega

theorem decOuts_ok (d : Design) (j k : Nat) : ∀ (outs : List Nat) (i lo : Nat) (tail : List (List Tok)),
    (∀ w ∈ outs, 0 < w) →
    decRepeat (fun _ => decOut) outs.length i ((outLines d j k i lo outs).map TLine.toks ++ tail) = some (outs, tail)
  | [], _, _, _, _ => rfl
  | w :: ws, i, lo, tail, hp => by
    have hw : 0 < w := hp w (by simp)
    have ih := decOuts_ok d j k ws (i + 1) (lo + w) tail (fun v hv => hp v (by simp [hv]))
    simp only [outLines, List.map_cons, List.cons_append, List.length_cons, decRepeat]
    rw [decOut_ok d j k i lo w hw]
    simp only [Option.bind_eq_bind, Option.bind_some]
    rw [ih]
    rfl

theorem decArgs_ok (d : Design) (j : Nat) (args : List Nat) (tail : List (List Tok)) :
    decRepeat (fun _ => decArg) args.length 0
      ((args.zipIdx.flatMap fun p => [argLine d j p]).map TLine.toks ++ tail) = some (args, tail) := by
  have := lines_ok decArg (fun i a => argLine d j (a, i)) id (fun _ => True)
    (fun i x tail _ => by simp [decArg, oneLine, argLine, TLine.toks, TW.wire, dotTW]) args (fun _ _ => trivial) tail
  simpa using this

theorem decCell_ok (d : Design) (m : Module) (k : Nat) (c : Cell) (hw : 0 < c.width) (hp : c.op.positive = true)
    (tail : List (List Tok)) :
    decCell ((cellLines d m k c).map TLine.toks ++ tail) = some (c, tail) := by
  obtain ⟨w, op⟩ := c
  cases op with
  | inst j args outs =>
    have hpos : ∀ v ∈ outs, 0 < v := by simpa [Op.positive] using hp
    have ha := decArgs_ok d j args
      ((outLines d j k 0 0 outs).map TLine.toks ++ ([TLine.toks ⟨lit! ")", [lit! ";"]⟩] ++ tail))
    have ho := decOuts_ok d j k outs 0 0 ([TLine.toks ⟨lit! ")", [lit! ";"]⟩] ++ tail) hpos
    simp only [cellLines, List.map_cons, List.map_append, List.cons_append, List.append_assoc, List.nil_append]
    simp only [decCell, TLine.toks, range, TW.num, TW.wire, List.map_cons, List.map_nil, List.cons_append,
      List.nil_append, List.map_append, List.length_cons, Nat.sub_add_cancel hw]
    simp only [decInst, oneLine, TW.module, TW.inst, Option.map_some, Option.bind_eq_bind, Option.bind_some,
      fixedLine, if_true]
    simp only [TLine.toks, List.map_nil, List.map_cons, List.singleton_append] at ha ho
    rw [ha]
    simp only [Option.bind_some]
    rw [ho]
    simp [oneLine]
  | _ =>
    simp only [cellLines, List.map_cons, List.map_nil, List.cons_append, List.nil_append]
    simp only [decCell, TLine.toks, range, TW.num, TW.wire, List.map_cons, List.map_append, List.cons_append,
      List.nil_append, List.map_nil, Nat.sub_add_cancel hw]
    rw [decodeExpr_ok m w _ hp (by intros; simp)]
    rfl


theorem zipWith_map_self {β γ δ α : Type} (f : γ → δ → α) (g : β → γ) (h : β → δ) :
    ∀ (xs : List β), List.zipWith f (xs.map g) (xs.map h) = xs.map (fun x => f (g x) (h x))
  | [] => rfl
  | x :: xs => by simp [zipWith_map_self f g h xs]

theorem decModule_ok (d : Design) (k : Nat) (m : Module) (hm : m.positive = true) (tail : List (List Tok)) :
    decModule k ((moduleLines d k m).map TLine.toks ++ tail) = some (m, tail) := by
  simp only [Module.positive, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hm
  obtain ⟨⟨⟨hin, hcell⟩, hreg⟩, hmem⟩ := hm
  have h_in := fun tail => lines_ok decInput inputLine id (fun p => 0 < p.width)
    (fun i x t h => decInput_ok i x t h) m.inputs.toList hin tail
  have h_out := fun tail => lines_ok skipLine (outputDecl m) (fun _ => ()) (fun _ => True)
    (fun _ _ _ _ => rfl) m.outputs.toList (fun _ _ => trivial) tail
  have h_rd := fun tail => lines_ok decRegDecl regDecl (fun r => (r.name, r.width)) (fun r => 0 < r.width)
    (fun i x t h => decRegDecl_ok i x t h) m.regs.toList hreg tail
  have h_md := fun tail => lines_ok decMemDecl memDecl (fun mm => (mm.name, mm.width, mm.addrWidth))
    (fun mm => 0 < mm.width) (fun i x t h => decMemDecl_ok i x t h) m.mems.toList hmem tail
  have h_cells := fun tail => decRepeat_ok (fun _ => decCell) (fun i c => (cellLines d m i c).map TLine.toks) id
    (fun c => 0 < c.width ∧ c.op.positive = true) (fun i x t h => decCell_ok d m i x h.1 h.2 t) m.cells.toList 0 tail
    (fun c hc => by simpa using hcell c hc)
  have h_ra := fun tail => lines_ok decRegAlways regAlways (fun r => (r.reset, r.next)) (fun _ => True)
    (fun i x t _ => decRegAlways_ok i x t) m.regs.toList (fun _ _ => trivial) tail
  have h_mb := fun tail => decRepeat_ok (fun _ => decMemBlock) (fun i mm => (memBlock i mm).map TLine.toks)
    (fun mm => mm.writes.toList) (fun _ => True) (fun i x t _ => decMemBlock_ok i x t) m.mems.toList 0 tail
    (fun _ _ => trivial)
  have h_as := fun tail => lines_ok decAssign assignLine id (fun _ => True)
    (fun i x t _ => decAssign_ok i x t) m.outputs.toList (fun _ _ => trivial) tail
  simp only [Array.length_toList, List.map_id] at h_in h_out h_rd h_md h_cells h_ra h_mb h_as
  simp only [moduleLines, List.map_append, List.map_cons, List.map_nil, List.append_assoc, List.cons_append,
    List.nil_append]
  simp only [← List.map_flatMap] at h_cells h_mb
  simp only [decModule, oneLine, headerLine, TLine.toks, TW.num, TW.module, List.map_cons, List.map_nil,
    Option.map_some, Option.bind_eq_bind, Option.bind_some, fixedLine, if_true]
  rw [h_in]; simp only [Option.bind_some]
  rw [h_out]; simp only [Option.bind_some, Option.map_some, if_true]
  rw [h_rd]; simp only [Option.bind_some]
  rw [h_md]; simp only [Option.bind_some]
  rw [h_cells]; simp only [Option.bind_some]
  rw [h_ra]; simp only [Option.bind_some]
  rw [h_mb]; simp only [Option.bind_some]
  rw [h_as]; simp only [Option.bind_some, Option.map_some, if_true, pure]
  rw [zipWith_map_self, zipWith_map_self]
  cases m
  simp


theorem decDesign_ok_tail (d : Design) (hd : d.positive = true) (tail : List (List Tok)) :
    decDesign ((designLines d).map TLine.toks ++ tail) = some (d, tail) := by
  have hms : ∀ m ∈ d.modules.toList, m.positive = true := by
    unfold Design.positive at hd
    exact List.all_eq_true.mp hd
  have h := decRepeat_ok decModule (fun i m => (moduleLines d i m).map TLine.toks) id (fun m => m.positive = true)
    (fun i x t h => decModule_ok d i x h t) d.modules.toList 0 tail hms
  simp only [Array.length_toList, List.map_id, ← List.map_flatMap] at h
  simp only [designLines, List.map_cons, List.cons_append, decDesign, oneLine, TLine.toks, TW.num, List.map_nil,
    Option.map_some, Option.bind_eq_bind, Option.bind_some]
  rw [h]
  simp

theorem decDesign_ok (d : Design) (hd : d.positive = true) :
    decDesign ((designLines d).map TLine.toks) = some (d, []) := by
  simpa using decDesign_ok_tail d hd []

/-- Printing then parsing returns the design, for every design whose printed
widths are positive (every well-formed design). -/
theorem _root_.HDLean.Rtl.Design.parse_systemVerilog (d : Design) (hd : d.positive = true) :
    Design.parseSystemVerilog d.systemVerilog = some d := by
  unfold Design.parseSystemVerilog
  rw [Design.systemVerilog, lex_render, decDesign_ok d hd]
  simp [Design.systemVerilog]

/-- Parsing accepts only the exact printing of what it returns. -/
theorem _root_.HDLean.Rtl.Design.parseSystemVerilog_canonical (text : String) (d : Design)
    (h : Design.parseSystemVerilog text = some d) : d.systemVerilog = text := by
  unfold Design.parseSystemVerilog at h
  split at h
  · split at h
    · rename_i heq; cases h; exact heq
    · cases h
  · cases h

end HDLean.Rtl.SV
