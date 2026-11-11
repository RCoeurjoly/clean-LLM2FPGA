import HDLean.Rtl.RTLIL

/-!
# Round trip of word-level RTLIL

`Design.parse_rtlil`: for every design whose printed widths are positive and
whose constants fit their widths (every design that passes `Design.check`),
parsing the printed RTLIL returns the design.
-/
namespace HDLean.Rtl.IL
open Emit Lex SV

set_option linter.unusedSimpArgs false

/-- Constants and reset values fit their widths; printed widths are positive. -/
def _root_.HDLean.Rtl.Module.rtlilOk (m : Module) : Bool :=
  m.positive &&
  m.cells.toList.all (fun c => match c.op with | .const v => decide (v < 2 ^ c.width) | _ => true) &&
  m.regs.toList.all (fun r => match r.reset with | .sync v => decide (v < 2 ^ r.width) | .none => true)

def _root_.HDLean.Rtl.Design.rtlilOk (d : Design) : Bool := d.modules.toList.all Module.rtlilOk

/-! ## Cells -/

theorem ofConnect_const (w v : Nat) (hv : v < 2 ^ w) : ofConnect (toks (constSig w v)) = some (.const v) := by
  simp [ofConnect, decConst_sig w v hv]

theorem argOf_lit (s : String) (x : List Tok) : argOf (.lit s, x) = none := rfl
theorem outOf_lit (s : String) (x : List Tok) : outOf (.lit s, x) = none := rfl

theorem argOf_conns (d : Design) (j : Nat) : ∀ (args : List Nat) (base : Nat),
    (((args.zipIdx base).map (fun p => (W.input p.2 (subInName d j p.2), [W.wire p.1]))).map
      (fun c : TW × List TW => (c.1.tok, toks c.2))).filterMap argOf = args
  | [], _ => rfl
  | a :: as, base => by
    simp only [List.zipIdx_cons, List.map_cons, List.filterMap_cons]
    rw [argOf_conns d j as (base + 1)]
    rfl

theorem argOf_outs (d : Design) (j k : Nat) : ∀ (outs : List Nat) (i lo : Nat),
    ((outConns d j k i lo outs).map (fun c => (c.1.tok, toks c.2))).filterMap argOf = []
  | [], _, _ => rfl
  | w :: ws, i, lo => by
    simp only [outConns, List.map_cons, List.filterMap_cons]
    exact argOf_outs d j k ws (i + 1) (lo + w)

theorem outOf_outs (d : Design) (j k : Nat) : ∀ (outs : List Nat) (i lo : Nat), (∀ w ∈ outs, 0 < w) →
    ((outConns d j k i lo outs).map (fun c => (c.1.tok, toks c.2))).filterMap outOf = outs
  | [], _, _, _ => rfl
  | w :: ws, i, lo, hp => by
    have hw : 0 < w := hp w (by simp)
    simp only [outConns, List.map_cons, List.filterMap_cons]
    rw [outOf_outs d j k ws (i + 1) (lo + w) (fun v hv => hp v (by simp [hv]))]
    simp only [W.output, W.wire, toks, List.map_cons, List.map_nil, TW.num, outOf]
    congr; omega

theorem outOf_args (d : Design) (j : Nat) : ∀ (args : List Nat) (base : Nat),
    (((args.zipIdx base).map (fun p => (W.input p.2 (subInName d j p.2), [W.wire p.1]))).map
      (fun c : TW × List TW => (c.1.tok, toks c.2))).filterMap outOf = []
  | [], _ => rfl
  | a :: as, base => by
    simp only [List.zipIdx_cons, List.map_cons, List.filterMap_cons]
    exact outOf_args d j as (base + 1)

theorem decCell_ok (d : Design) (m : Module) (k : Nat) (c : Cell) (hw : 0 < c.width) (hp : c.op.positive = true)
    (hc : (match c.op with | .const v => decide (v < 2 ^ c.width) | _ => true) = true) (rest : List Stmt) :
    decCell ((cellStmts d m k c).map PStmt.erase ++ rest) = some (c, rest) := by
  obtain ⟨w, op⟩ := c
  cases op with
  | const v =>
    simp only [decide_eq_true_eq] at hc
    simp [cellStmts, cellBody, PStmt.erase, decCell, decBody, W.wire, ofConnect_const w v hc]
  | bin o a b => cases o <;> rfl
  | cmp o cw a b => cases o <;> rfl
  | slice a lo => rfl
  | concat h l lw =>
    have hlw : 0 < lw := by simpa [Op.positive] using hp
    simp only [cellStmts, cellBody, PStmt.erase, decCell, decBody, List.map_cons, List.map_nil, List.cons_append,
      List.nil_append, Option.map_some]
    simp [ofConnect, decConst, bitsVal, toks, W.wire, TW.num, Nat.sub_add_cancel hlw]
  | inst j args outs =>
    have hpos : ∀ v ∈ outs, 0 < v := by simpa [Op.positive] using hp
    have h1 : decCell ((cellStmts d m k ⟨w, .inst j args outs⟩).map PStmt.erase ++ rest) =
        (ofCell (.named 'h' j (subName d j)) [] ((instConns d j k args outs).map fun c => (c.1.tok, toks c.2))).map
          fun op => (⟨w, op⟩, rest) := rfl
    rw [h1]
    have h2 : ∀ cs, ofCell (.named 'h' j (subName d j)) [] cs = some (.inst j (cs.filterMap argOf) (cs.filterMap outOf)) :=
      fun _ => rfl
    rw [h2]
    simp only [instConns, List.map_append, List.map_cons, List.map_nil, List.cons_append, List.nil_append,
      List.filterMap_cons, List.filterMap_append, argOf_lit, outOf_lit, argOf_conns, argOf_outs,
      outOf_outs d j k outs 0 0 hpos, outOf_args, List.append_nil, List.nil_append]
    rfl
  | _ => rfl

/-! ## Registers and memories -/

theorem decReg_ok (k : Nat) (r : Reg) (hr : (match r.reset with | .sync v => decide (v < 2 ^ r.width) | .none => true) = true)
    (rest : List Stmt) : decReg ((regStmts k r).map PStmt.erase ++ rest) = some ((r.reset, r.next), rest) := by
  obtain ⟨name, w, reset, next⟩ := r
  cases reset with
  | none => rfl
  | sync v =>
    simp only [decide_eq_true_eq] at hr
    have h : decReg ((regStmts k ⟨name, w, .sync v, next⟩).map PStmt.erase ++ rest) =
        (do let n ← some next; let v ← decConst (toks (constSig w v)); pure ((Reset.sync v, n), rest)) := rfl
    rw [h, decConst_sig w v hr]
    rfl

theorem decWrite_ok (k : Nat) (mm : Mem) (j : Nat) (p : WritePort) (hw : 0 < mm.width) (rest : List Stmt) :
    decWrite ((writeStmt k mm j p).erase :: rest) = some (p, rest) := by
  obtain ⟨w', hw'⟩ : ∃ w', mm.width = w' + 1 := ⟨mm.width - 1, by omega⟩
  have h : enOf (some (toks (enSig mm.width p.en))) = some p.en := by
    rw [hw']; rfl
  have e : decWrite ((writeStmt k mm j p).erase :: rest) =
      (do let en ← enOf (some (toks (enSig mm.width p.en))); let a ← some p.addr; let dt ← some p.data;
          pure ((⟨en, a, dt⟩ : WritePort), rest)) := rfl
  rw [e, h]
  rfl

theorem sRepeat_ok (d : Nat → SDec α) (enc : Nat → β → List Stmt) (g : β → α) (P : β → Prop)
    (h : ∀ i x tail, P x → d i (enc i x ++ tail) = some (g x, tail)) :
    ∀ (xs : List β) (base : Nat) (tail : List Stmt), (∀ x ∈ xs, P x) →
      sRepeat d xs.length base ((xs.zipIdx base).flatMap (fun p => enc p.2 p.1) ++ tail) = some (xs.map g, tail)
  | [], _, _, _ => rfl
  | x :: xs, base, tail, hp => by
    simp only [List.length_cons, sRepeat, List.zipIdx_cons, List.flatMap_cons, List.append_assoc]
    rw [h base x _ (hp x (by simp))]
    simp only [Option.bind_eq_bind, Option.bind_some]
    rw [sRepeat_ok d enc g P h xs (base + 1) tail (fun y hy => hp y (by simp [hy]))]
    rfl

theorem flatMap_single (l : List α) (f : α → β) : l.flatMap (fun x => [f x]) = l.map f := by
  induction l <;> simp_all

/-- Single-statement items. -/
theorem items_ok (f : Stmt → Option α) (enc : Nat → β → PStmt) (g : β → α) (P : β → Prop)
    (h : ∀ i x, P x → f (enc i x).erase = some (g x)) (xs : List β) (hp : ∀ x ∈ xs, P x) (tail : List Stmt) :
    sRepeat (fun _ => sOne f) xs.length 0 ((xs.zipIdx.map fun p => enc p.2 p.1).map PStmt.erase ++ tail) =
      some (xs.map g, tail) := by
  have := sRepeat_ok (fun _ => sOne f) (fun i x => [(enc i x).erase]) g P
    (fun i x tail hx => by simp [sOne, h i x hx]) xs 0 tail hp
  rw [flatMap_single] at this
  simpa [List.map_map, Function.comp_def] using this

theorem decWrites_ok (k : Nat) (mm : Mem) (hw : 0 < mm.width) (rest : List Stmt) :
    decWrites ((writeStmts k mm).map PStmt.erase ++ rest) = some (mm.writes.toList, rest) := by
  have hr := sRepeat_ok (fun _ => decWrite) (fun j p => [(writeStmt k mm j p).erase]) id (fun _ => True)
    (fun i x tail _ => decWrite_ok k mm i x hw tail) mm.writes.toList 0 rest (fun _ _ => trivial)
  simp only [List.map_id, Array.length_toList] at hr
  simp only [writeStmts, List.map_cons, List.cons_append, decWrites, sOne, PStmt.erase, toks, List.map_cons,
    List.map_nil, TW.num, Option.map_some, Option.bind_eq_bind, Option.bind_some]
  have e : (mm.writes.toList.zipIdx.map fun q => writeStmt k mm q.2 q.1).map PStmt.erase =
      mm.writes.toList.zipIdx.flatMap fun q => [(writeStmt k mm q.2 q.1).erase] := by
    rw [flatMap_single, List.map_map]; rfl
  rw [e]
  simpa using hr

/-! ## Modules and designs -/

theorem decModule_ok (d : Design) (k : Nat) (m : Module) (hm : m.rtlilOk = true) (tail : List Stmt) :
    decModule k ((moduleStmts d k m).map PStmt.erase ++ tail) = some (m, tail) := by
  simp only [Module.rtlilOk, Module.positive, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hm
  obtain ⟨⟨⟨⟨⟨_, hcell⟩, _⟩, hmem⟩, hconst⟩, hreset⟩ := hm
  have h_in := fun tail => items_ok inputOf (fun i (p : Port) => .wire p.width (.input (i + 3)) (W.input i p.name)) id (fun _ => True)
    (fun _ _ _ => rfl) m.inputs.toList (fun _ _ => trivial) tail
  have h_out := fun tail => items_ok outputNameOf (fun i (o : OutPort) => .wire (m.cellWidth o.node) (.output (m.inputs.size + 3 + i)) (W.output i o.name))
    OutPort.name (fun _ => True) (fun _ _ _ => rfl) m.outputs.toList (fun _ _ => trivial) tail
  have h_rd := fun tail => items_ok regDeclOf (fun i (r : Reg) => .wire r.width .none (W.reg i r.name)) (fun r => (r.name, r.width))
    (fun _ => True) (fun _ _ _ => rfl) m.regs.toList (fun _ _ => trivial) tail
  have h_md := fun tail => sRepeat_ok (fun _ => decMemDecl) (fun i mm => (memStmts i mm).map PStmt.erase)
    (fun mm => (mm.name, mm.width, mm.addrWidth)) (fun _ => True) (fun _ _ _ _ => rfl) m.mems.toList 0 tail
    (fun _ _ => trivial)
  have h_cells := fun tail => sRepeat_ok (fun _ => decCell) (fun i c => (cellStmts d m i c).map PStmt.erase) id
    (fun c => 0 < c.width ∧ c.op.positive = true ∧
      (match c.op with | .const v => decide (v < 2 ^ c.width) | _ => true) = true)
    (fun i x t h => decCell_ok d m i x h.1 h.2.1 h.2.2 t) m.cells.toList 0 tail
    (fun c hc => ⟨(hcell c hc).1, (hcell c hc).2, hconst c hc⟩)
  have h_ra := fun tail => sRepeat_ok (fun _ => decReg) (fun i r => (regStmts i r).map PStmt.erase)
    (fun r => (r.reset, r.next))
    (fun r => (match r.reset with | .sync v => decide (v < 2 ^ r.width) | .none => true) = true)
    (fun i x t h => decReg_ok i x h t) m.regs.toList 0 tail hreset
  have h_mw := fun tail => sRepeat_ok (fun _ => decWrites) (fun i mm => (writeStmts i mm).map PStmt.erase)
    (fun mm => mm.writes.toList) (fun mm => 0 < mm.width) (fun i x t h => decWrites_ok i x h t) m.mems.toList 0 tail
    hmem
  have h_as := fun tail => items_ok outputNodeOf (fun i (o : OutPort) => .connect (W.output i o.name) [W.wire o.node]) OutPort.node
    (fun _ => True) (fun _ _ _ => rfl) m.outputs.toList (fun _ _ => trivial) tail
  simp only [Array.length_toList, List.map_id] at h_in h_out h_rd h_md h_cells h_ra h_mw h_as
  simp only [moduleStmts, List.map_append, List.map_cons, List.map_nil, List.append_assoc, List.cons_append,
    List.nil_append, List.map_flatMap]
  simp only [decModule, sOne, headerOf, moduleNameOf, PStmt.erase, toks, List.map_cons, List.map_nil, TW.num,
    W.module, Option.map_some, Option.bind_eq_bind, Option.bind_some]
  rw [h_in]; simp only [Option.bind_some]
  rw [h_out]; simp only [Option.bind_some]
  rw [h_rd]; simp only [Option.bind_some]
  rw [h_md]; simp only [Option.bind_some]
  rw [h_cells]; simp only [Option.bind_some]
  rw [h_ra]; simp only [Option.bind_some]
  rw [h_mw]; simp only [Option.bind_some]
  rw [h_as]; simp only [Option.bind_some, sOne, endOf, Option.map_some, pure]
  rw [zipWith_map_self, zipWith_map_self, zipWith_map_self]
  cases m
  simp

theorem decDesign_ok_tail (d : Design) (hd : d.rtlilOk = true) (tail : List Stmt) :
    decDesign ((designStmts d).map PStmt.erase ++ tail) = some (d, tail) := by
  have hms : ∀ m ∈ d.modules.toList, m.rtlilOk = true := List.all_eq_true.mp hd
  have h := sRepeat_ok decModule (fun i m => (moduleStmts d i m).map PStmt.erase) id (fun m => m.rtlilOk = true)
    (fun i x t h => decModule_ok d i x h t) d.modules.toList 0 tail hms
  simp only [Array.length_toList, List.map_id] at h
  simp only [designStmts, List.map_cons, List.cons_append, List.map_flatMap, decDesign, sOne, PStmt.erase, toks,
    List.map_cons, List.map_nil, TW.num, Option.map_some, Option.bind_eq_bind, Option.bind_some]
  rw [h]
  simp

theorem decDesign_ok (d : Design) (hd : d.rtlilOk = true) :
    decDesign ((designStmts d).map PStmt.erase) = some (d, []) := by
  simpa using decDesign_ok_tail d hd []

/-- Printing then parsing returns the design, for every design whose printed
widths are positive and whose constants fit their widths. -/
theorem _root_.HDLean.Rtl.Design.parse_rtlil (d : Design) (hd : d.rtlilOk = true) :
    Design.parseRTLIL d.rtlil = some d := by
  unfold Design.parseRTLIL
  rw [Design.rtlil, lexStmts_render, Option.bind_some, decDesign_ok d hd]
  simp [Design.rtlil]

/-- Parsing accepts only the exact printing of what it returns. -/
theorem _root_.HDLean.Rtl.Design.parseRTLIL_canonical (text : String) (d : Design)
    (h : Design.parseRTLIL text = some d) : d.rtlil = text := by
  unfold Design.parseRTLIL at h
  split at h
  · split at h
    · rename_i heq; cases h; exact heq
    · cases h
  · cases h

end HDLean.Rtl.IL
