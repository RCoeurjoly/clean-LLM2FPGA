import HDLean.Backend.AigerProved
import HDLean.Backend.TextGrammar

set_option backward.isDefEq.respectTransparency false
namespace HDLean.Backend.Proved
open Text Text.Grammar

private abbrev Block (α : Type) := Grammar Line (List String) α Line.tokens
private def svPortsRow (moduleName : Word) : Row (Nat × Nat) :=
  before [⟨"module", by decide, by decide⟩, moduleName, ⟨"(", by decide, by decide⟩, ⟨"input", by decide, by decide⟩, ⟨"wire", by decide, by decide⟩, ⟨"clk", by decide, by decide⟩, ⟨",", by decide, by decide⟩, ⟨"input", by decide, by decide⟩, ⟨"wire", by decide, by decide⟩, ⟨"[", by decide, by decide⟩] (pair (after natural [⟨":", by decide, by decide⟩, ⟨"0", by decide, by decide⟩, ⟨"]", by decide, by decide⟩, ⟨"ins", by decide, by decide⟩, ⟨",", by decide, by decide⟩, ⟨"output", by decide, by decide⟩, ⟨"wire", by decide, by decide⟩, ⟨"[", by decide, by decide⟩]) (before [] (after natural [⟨":", by decide, by decide⟩, ⟨"0", by decide, by decide⟩, ⟨"]", by decide, by decide⟩, ⟨"outs", by decide, by decide⟩, ⟨")", by decide, by decide⟩, ⟨";", by decide, by decide⟩])))

private def svPorts (moduleName : Word) : Block (Nat × Nat) := line (svPortsRow moduleName) (by intro a; simp [svPortsRow, before, after, pair, Grammar.map, fixed, natural])

private def svRegsRow : Row (Nat) :=
  before [⟨"wire", by decide, by decide⟩, ⟨"[", by decide, by decide⟩] (after natural [⟨":", by decide, by decide⟩, ⟨"0", by decide, by decide⟩, ⟨"]", by decide, by decide⟩, ⟨"state", by decide, by decide⟩, ⟨";", by decide, by decide⟩])

private def svRegs : Block (Nat) := line svRegsRow (by intro a; simp [svRegsRow, before, after, pair, Grammar.map, fixed, natural])

macro "svw!" s:str : term => `((⟨$s,by decide,by decide⟩ : HDLean.Backend.Text.Word))

private def net (k : Nat) : Word := prefixed (svw!"n_") k
private def stateNet (k : Nat) : Word := prefixed (svw!"s_") k
private def gateNet (k : Nat) : Word := prefixed (svw!"g_") k
private def literalRef : Row Nat := numbered (svw!"n_")

private def svGatesRow : Row Nat :=
  before [svw!"localparam",svw!"HD_GATES",svw!"="] (after natural [svw!";"])
private def svGates : Block Nat := line svGatesRow (by intro a; simp [svGatesRow,before,after,pair,Grammar.map,fixed,natural])

private def svInvRow (k : Nat) : Row Unit :=
  fixed [svw!"wire",net (2*k+1),svw!"=",svw!"~",net (2*k),svw!";"]
private def svInv (k : Nat) : Block Unit := line (svInvRow k) (by intro a; simp [svInvRow,fixed])

private def svInputRow (k : Nat) : Row Unit :=
  fixed [svw!"wire",net (2*(k+1)),svw!"=",svw!"ins",svw!"[",numeral k,svw!"]",svw!";"]
private def svInput (k : Nat) : Block Unit := line (svInputRow k) (by intro a; simp [svInputRow,fixed])

private def svState (i k : Nat) : Block Unit := fixed [
  ⟨[svw!"reg",stateNet k,svw!";"],by simp⟩,
  ⟨[svw!"wire",net (2*(i+k+1)),svw!"=",stateNet k,svw!";"],by simp⟩,
  ⟨[svw!"assign",svw!"state",svw!"[",numeral k,svw!"]",svw!"=",stateNet k,svw!";"],by simp⟩]

private def svNextRow (k : Nat) : Row Nat :=
  before [svw!"always",svw!"@",svw!"(",svw!"posedge",svw!"clk",svw!")",stateNet k,svw!"<="]
    (after literalRef [svw!";"])
private def svNext (k : Nat) : Block Nat := line (svNextRow k) (by intro a; simp [svNextRow,before,after,pair,Grammar.map,fixed])

private def svOutputRow (k : Nat) : Row Nat :=
  before [svw!"assign",svw!"outs",svw!"[",numeral k,svw!"]",svw!"="] (after literalRef [svw!";"])
private def svOutput (k : Nat) : Block Nat := line (svOutputRow k) (by intro a; simp [svOutputRow,before,after,pair,Grammar.map,fixed])

private def svGateRow (k : Nat) : Row (Nat × Nat) :=
  before [svw!"wire",gateNet k,svw!"="]
    (pair (after literalRef [svw!"&"]) (after literalRef [svw!";"]))
private def svGateTargetRow (k : Nat) : Row Nat :=
  before [svw!"wire"] (after literalRef [svw!"=",gateNet k,svw!";"])
private def svGate (k : Nat) : Block Gate :=
  ((pair (line (svGateRow k) (by intro a; simp [svGateRow,before,after,pair,Grammar.map,fixed]))
    (line (svGateTargetRow k) (by intro a; simp [svGateTargetRow,before,after,pair,Grammar.map,fixed]))).checkedMap
    (fun p => ⟨p.2 / 2,p.1.1,p.1.2⟩) (fun g => ((g.a,g.b),2*g.lhs))
    (by intro g; cases g; simp))

private def svHeader (moduleName : Word) : Block Header :=
  (pair (svPorts moduleName) (pair svRegs svGates)).map
    (fun p => ⟨p.1.1,p.2.1,p.1.2,p.2.2⟩)
    (fun h => ((h.inputs,h.outputs),(h.latches,h.gates))) (by intro h; cases h; rfl)

private def svConstants (h : Header) : Block Unit := fixed [
  ⟨[svw!"localparam",svw!"HD_LITERALS",svw!"=",numeral (2*(h.inputs+h.latches+h.gates)+1),svw!";"],by simp⟩,
  ⟨[svw!"wire",net 0,svw!"=",svw!"1'b0",svw!";"],by simp⟩,
  ⟨[svw!"wire",net 1,svw!"=",svw!"1'b1",svw!";"],by simp⟩,
  ⟨[svw!"assign",svw!"state",svw!"[",numeral h.latches,svw!"]",svw!"=",svw!"1'b0",svw!";"],by simp⟩,
  ⟨[svw!"assign",svw!"outs",svw!"[",numeral h.outputs,svw!"]",svw!"=",svw!"1'b0",svw!";"],by simp⟩]

private def svEnd : Block Unit := fixed [⟨[⟨"endmodule", by decide, by decide⟩], by simp⟩]

/-- Scalar literal wires avoid quadratic HDL elaboration of huge packed buses.
Arbitrary state has no initializer. -/
def Aag.svLinesNamed (moduleName : Word) (g : Aag) : List Line :=
  let h : Header := ⟨g.inputs,g.next.length,g.outputs.length,g.gates.length⟩
  (svHeader moduleName).encode h ++ (svConstants h).encode () ++
  repeatEncode svInput 0 (List.replicate g.inputs ()) ++
  repeatEncode (svState g.inputs) 0 (List.replicate g.next.length ()) ++
  repeatEncode svInv 1 (List.replicate (g.inputs+g.next.length+g.gates.length) ()) ++
  repeatEncode svGate 0 g.gates ++ repeatEncode svOutput 0 g.outputs ++
  repeatEncode svNext 0 g.next ++ svEnd.encode ()

private def topWord : Word := ⟨"hd_top",by decide,by decide⟩
private def numberedWord (id : Nat) : Word := prefixed ⟨"hd_m_",by decide,by decide⟩ id

def Aag.svLines (g : Aag) : List Line := g.svLinesNamed topWord

def Aag.systemVerilog (g : Aag) : String := Text.render g.svLines

/-- Module names are generated from naturals, never interpolated from unchecked text. -/
def Aag.systemVerilogNamed (id : Nat) (g : Aag) : String := Text.render (g.svLinesNamed (numberedWord id))

private def readSVTokens (moduleName : Word) (tokens : List (List String)) : Option (Aag × List (List String)) := do
  let (h,rest) ← (svHeader moduleName).decode tokens
  let (_,rest) ← (svConstants h).decode rest
  let (_,rest) ← repeatDecode svInput h.inputs 0 rest
  let (_,rest) ← repeatDecode (svState h.inputs) h.latches 0 rest
  let (_,rest) ← repeatDecode svInv (h.inputs+h.latches+h.gates) 1 rest
  let (gs,rest) ← repeatDecode svGate h.gates 0 rest
  let (os,rest) ← repeatDecode svOutput h.outputs 0 rest
  let (ns,rest) ← repeatDecode svNext h.latches 0 rest
  let (_,rest) ← svEnd.decode rest
  return (⟨h.inputs,ns,os,gs⟩,rest)

private def parseSVTokens (moduleName : Word) (tokens : List (List String)) : Option Aag := do
  let (g,rest) ← readSVTokens moduleName tokens
  if rest.isEmpty then return g else none

def Aag.parseSystemVerilog (text : String) : Option Aag := parseSVTokens topWord (Text.tokenize text)

def Aag.parseSystemVerilogNamed (id : Nat) (text : String) : Option Aag :=
  parseSVTokens (numberedWord id) (Text.tokenize text)

private theorem sv_read_roundtrip (moduleName : Word) (g : Aag) (tail : List (List String)) :
    readSVTokens moduleName ((g.svLinesNamed moduleName).map Line.tokens ++ tail) = some (g,tail) := by
  simp only [Aag.svLinesNamed, List.map_append, List.append_assoc, readSVTokens,
    Grammar.roundtrip]
  dsimp only [Bind.bind, Option.bind]
  rw [Grammar.roundtrip]
  dsimp only [Bind.bind, Option.bind]
  have hi := repeat_roundtrip svInput (List.replicate g.inputs ()) 0
  have hr := repeat_roundtrip (svState g.inputs) (List.replicate g.next.length ()) 0
  have hv := repeat_roundtrip svInv (List.replicate (g.inputs+g.next.length+g.gates.length) ()) 1
  simp only [List.length_replicate] at hi hr hv
  rw [hi]
  dsimp only [Bind.bind, Option.bind]
  rw [hr]
  dsimp only [Bind.bind, Option.bind]
  rw [hv]
  dsimp only [Bind.bind, Option.bind]
  rw [repeat_roundtrip]
  dsimp only [Bind.bind, Option.bind]
  rw [repeat_roundtrip]
  dsimp only [Bind.bind, Option.bind]
  rw [repeat_roundtrip]
  dsimp only [Bind.bind, Option.bind]
  have he := svEnd.roundtrip () tail
  rw [he]
  cases g
  rfl

private theorem sv_roundtrip (moduleName : Word) (g : Aag) :
    parseSVTokens moduleName (Text.tokenize (Text.render (g.svLinesNamed moduleName))) = some g := by
  rw [Text.tokenize_render]
  unfold parseSVTokens
  have h := sv_read_roundtrip moduleName g []
  simp only [List.append_nil] at h
  rw [h]
  rfl

def Aag.systemVerilogModuleLines (id : Nat) (g : Aag) : List Line := g.svLinesNamed (numberedWord id)
def Aag.readSystemVerilogModule (id : Nat) : List (List String) → Option (Aag × List (List String)) :=
  readSVTokens (numberedWord id)

theorem Aag.read_systemVerilogModule (id : Nat) (g : Aag) (tail : List (List String)) :
    Aag.readSystemVerilogModule id ((g.systemVerilogModuleLines id).map Line.tokens ++ tail) = some (g,tail) :=
  sv_read_roundtrip (numberedWord id) g tail

theorem Aag.parse_systemVerilog (g : Aag) : Aag.parseSystemVerilog g.systemVerilog = some g :=
  sv_roundtrip topWord g

theorem Aag.parse_systemVerilogNamed (id : Nat) (g : Aag) :
    Aag.parseSystemVerilogNamed id (g.systemVerilogNamed id) = some g :=
  sv_roundtrip (numberedWord id) g

theorem systemVerilog_print_correct (g : Graph) (hn : g.next.size = g.latches) (inputs state : Array Bool) :
    (Aag.parseSystemVerilog (Aag.ofGraph g).systemVerilog).map
      (fun parsed => parsed.graph.eval inputs state) = some (g.eval inputs state) := by
  rw [Aag.parse_systemVerilog]
  exact congrArg some (Graph.eval_eq_of_logic_eq _ _ (Aag.ofGraph_logic g hn) inputs state)

end HDLean.Backend.Proved
