import Lean
import CircuitHdl.Core
import CircuitHdl.Width

/-!
Surface syntax for the circuit HDL.

Each surface form lives in its own syntax category and is lowered to the `CircuitHdl.Core`
AST through a bracketed term entry point, following the pattern used by the MLIR EDSL in
`LeanMLIR/MLIRSyntax/GenericParser.lean`. Keying the `macro_rules` on the bracketed forms is what
keeps them from firing on arbitrary terms.

The shape we parse:

```
module! counter (clk : 1, rst : 1) -> (q : 8) {
  reg q : 8;
  always @posedge(clk) {
    if rst { assign q = 0; }
    else   { assign q = q + 1; }
  }
}
```

### Operators

Loosest to tightest, following Verilog:

| Prec | Form |
|---|---|
| 20 | `c ? t : f` (right-associative) |
| 24 | `\|\|` logical |
| 26 | `&&` logical |
| 30 | `\|` bitwise |
| 32 | `^` bitwise |
| 35 | `&` bitwise |
| 40 | `==` `!=` |
| 45 | `<` `<=` `>` `>=` |
| 60 | `<< n` `>> n` (constant amount) |
| 65 | `+` |
| 75 | `~` bitwise not, `!` logical not (prefix) |
| max | ident, num, `( e )`, `( e : w )`, `{ e, .. }`, `e[hi:lo]`, `e[i]` |

As in Verilog, `&` `|` `^` `~` are **bitwise** and `&&` `||` `!` are **logical** -- the latter
reduce each operand to a truth value and yield one bit. Note that Verilog puts the logical operators
*looser* than the bitwise ones, where C puts `&&` between `^` and `==`.

Several forms desugar here rather than reaching the AST, so `Core` needs no constructor for them:

```
a != b  ==>  ~(a == b)            a > b   ==>  b < a
!a      ==>  a == 0               a && b  ==>  (~(a == 0)) & (~(b == 0))
```

Comparisons are unsigned.

### Widths

Widths are **inferred from declarations**. The target of an assignment fixes the expected width,
which is then pushed down through the expression: `reg q : 8;` is what makes `assign q = q + 1;`
produce `Expr.bin .add 8 (Expr.var `q) (Expr.lit 8 1)`. A disagreement is a compile error positioned
on the offending token.

Pushing down is not enough on its own, because three forms do not preserve width: a comparison is
always one bit, a concatenation is the sum of its operands, and a slice is fixed by its bounds. For
those, the expected width says nothing about the operands, so lowering is **bidirectional** --
`widthOf?` synthesises a width bottom-up, and `checkExpr` pushes one down, calling `widthOf?`
wherever it needs an operand width it cannot otherwise know.

A bare literal has no width of its own, so `widthOf?` returns `none` for it. That is only a problem
where nothing else determines the width -- `{3, a}` is rejected, exactly as Verilog rejects an
unsized literal in a concatenation. The ascription `(3 : 4)` supplies one.

`assign x : w = e` pins the width of a whole assignment. It is itself checked against the
declaration, so it can only ever restate it -- it exists to make a width visible at the point of
use, not to override the analysis.

Checking happens here, in `MacroM`, so it costs nothing at runtime. The same rules are stated as
ordinary Lean over the AST in `CircuitHdl.Width`, which is what validates hand-written
modules; the two share `CheckError` so their messages agree.
-/

namespace CircuitHdl

open Lean

declare_syntax_cat circuit_port
declare_syntax_cat circuit_expr
declare_syntax_cat circuit_stmt
declare_syntax_cat circuit_item
declare_syntax_cat circuit_binding
declare_syntax_cat circuit_field
declare_syntax_cat circuit_fields
declare_syntax_cat circuit_iface

/-! ### Interfaces

An AXI port is one thing, but spelling it as three loose wires records neither that they belong
together nor that `ready` runs *against* the other two. An interface says both:

```lean
macro_rules
  | `(circuit_iface| axis $dw:term) =>
      `(circuit_fields| fields! { fwd valid : 1, rev ready : 1, fwd data : $dw })
```

`fwd` runs with the port's direction and `rev` against it, which is the only thing that
distinguishes `ready` from `valid`.

An interface is declared with a `macro_rules` rather than a dedicated `interface!` command, and that
is a limitation rather than a preference. A command generating this would have to splice the
interface's name into the *pattern* of a nested quotation, and there is no way to do that: every
antiquotation inside `` `(macro_rules | `(cat| ..) => ..) `` belongs to the generated code, so the
outer name cannot reach it. Sugar for this needs a command elaborator building the syntax by hand.
-/

/-- One field of an interface. -/
syntax "fwd" ident ":" term : circuit_field
syntax "rev" ident ":" term : circuit_field

/-- What an interface reference expands to. -/
syntax "fields!" "{" circuit_field,* "}" : circuit_fields

/-- A reference to an interface: its name, then its arguments.

Deliberately one *generic* rule rather than a rule generated per interface, so that declaring an
interface reserves no tokens -- an `interface! data …` must not make `data` a keyword everywhere.
`interface!` generates only `macro_rules` matching its own name. -/
syntax ident term* : circuit_iface

/-- A port declaration, `a : 8`, or a whole interface, `slave s : axis dw`.

A slave port is incoming, so its `fwd` fields are inputs and its `rev` fields outputs; a master port
is the mirror. That is what makes `ready` flow the right way without being declared specially. -/
syntax ident ":" term : circuit_port
syntax "slave" ident ":" circuit_iface : circuit_port
syntax "master" ident ":" circuit_iface : circuit_port

syntax:max ident : circuit_expr
syntax:max num : circuit_expr
syntax:max "(" circuit_expr ")" : circuit_expr
syntax:max "(" circuit_expr ":" term ")" : circuit_expr
syntax:max "{" circuit_expr,+ "}" : circuit_expr
syntax:max circuit_expr:max "[" term ":" term "]" : circuit_expr
syntax:max circuit_expr:max "[" circuit_expr "]" : circuit_expr
syntax:75 "~" circuit_expr:75 : circuit_expr
syntax:75 "!" circuit_expr:75 : circuit_expr
syntax:65 circuit_expr:65 " + " circuit_expr:66 : circuit_expr
syntax:60 circuit_expr:60 " << " term:max : circuit_expr
syntax:60 circuit_expr:60 " >> " term:max : circuit_expr
syntax:45 circuit_expr:46 " < " circuit_expr:46 : circuit_expr
syntax:45 circuit_expr:46 " <= " circuit_expr:46 : circuit_expr
syntax:45 circuit_expr:46 " > " circuit_expr:46 : circuit_expr
syntax:45 circuit_expr:46 " >= " circuit_expr:46 : circuit_expr
syntax:40 circuit_expr:41 " == " circuit_expr:41 : circuit_expr
syntax:40 circuit_expr:41 " != " circuit_expr:41 : circuit_expr
syntax:35 circuit_expr:35 " & " circuit_expr:36 : circuit_expr
syntax:32 circuit_expr:32 " ^ " circuit_expr:33 : circuit_expr
syntax:30 circuit_expr:30 " | " circuit_expr:31 : circuit_expr
syntax:26 circuit_expr:26 " && " circuit_expr:27 : circuit_expr
syntax:24 circuit_expr:24 " || " circuit_expr:25 : circuit_expr
syntax:20 circuit_expr:21 " ? " circuit_expr " : " circuit_expr:20 : circuit_expr

syntax "assign" ident "=" circuit_expr ";" : circuit_stmt
syntax "assign" ident "[" circuit_expr "]" "=" circuit_expr ";" : circuit_stmt
syntax "assign" ident ":" term:51 "=" circuit_expr ";" : circuit_stmt
syntax "if" circuit_expr "{" circuit_stmt* "}" "else" "{" circuit_stmt* "}" : circuit_stmt

syntax "reg" ident ":" term ";" : circuit_item
syntax "reg" ident "[" term "]" ":" term ";" : circuit_item
syntax "wire" ident ":" term ";" : circuit_item
/-- A port binding, `.b(x)`. The bound signal is a name, not an expression. -/
syntax "." ident "(" ident ")" : circuit_binding
/-- An instance, `grayOf wg (.b(x), .g(y));`. -/
syntax ident ident "(" sepBy(circuit_binding, ",") ")" ";" : circuit_item
/-- An instance of a *parameterized* module, `(grayOfW (k+3)) wg (.b(x), .g(y));`.

The parentheses are required: an unparenthesized `grayOfW (k+3) wg` would parse as one function
application, leaving no way to tell the parameters from the instance name. -/
syntax "(" term ")" ident "(" sepBy(circuit_binding, ",") ")" ";" : circuit_item
/-- A derived value, `let n := Nat.log2 depth;`.

It scopes over the items that follow it, like a `let` anywhere else. This is how a module names a
width computed from its parameters; making it a defaulted parameter instead would let a caller
override it into disagreeing with the parameter it was derived from. -/
syntax "let" ident ":=" term ";" : circuit_item
syntax "always" "@(posedge" ident ")" "{" circuit_stmt* "}" : circuit_item
syntax "always" "@(negedge" ident ")" "{" circuit_stmt* "}" : circuit_item
syntax circuit_stmt : circuit_item

/-- Lower a `circuit_expr` against an explicit environment and expected width:

```lean
[circuit_expr (a : 1, b : 1) | a && b : 1]
```

This exists so expressions can be unit-tested on their own. Inside a module the environment comes
from the declarations instead. -/
syntax "[circuit_expr" "(" sepBy(circuit_port, ",") ")" "|" circuit_expr ":" term "]" : term

syntax "module!" ident
  "(" sepBy(circuit_port, ",") ")" "->" "(" sepBy(circuit_port, ",") ")"
  "{" circuit_item* "}" : term

/-! ### Macro-time width analysis

The rules mirror `CircuitHdl.Width`, but report against the source instead of returning an
`Except`, and carry widths as **Lean terms** rather than numbers so that a module can be a function
of its size.

That has a consequence worth being precise about. Two widths can be compared here only when the
comparison is decidable at macro time: concrete widths compare numerically -- which is what keeps
every error a fixed-size module reports today -- and two symbolic widths compare by syntax. Anything
else is deferred to `Module.check`, where the widths are ordinary `Nat`s. So parameterizing a module
does not weaken its checking so much as move it: `(m n).check = .ok ()` is a theorem about every
size, which is stronger than a macro checking one.
-/

/-- A width as the surface syntax knows it: a Lean term, possibly mentioning parameters. -/
abbrev SymWidth := TSyntax `term

/-- The macro's view of the declarations, the counterpart of `Width.Ctx` before elaboration. -/
structure SymCtx where
  env : List (Name × SymWidth) := []
  inputs : List Name := []
  /-- Memories, as `(name, depth, element width)`. -/
  mems : List (Name × SymWidth × SymWidth) := []

/-- Report a `CheckError` against a piece of syntax, so the message lands on the offending token. -/
private def throwWidth {α} (ref : Syntax) (e : CheckError) : MacroM α :=
  Macro.throwErrorAt ref e.render

/-- The value of a width, when it is a literal. -/
private def asNat? (w : SymWidth) : Option Nat := w.raw.isNatLit?

/-- A width term for a known number. -/
private def ofNat (n : Nat) : MacroM SymWidth := `($(Lean.quote n))

/-- Require two widths to agree, as far as that can be established here.

Only a disagreement between two *literals* is an error: two symbolic widths that merely look
different may still be equal, so rejecting them would turn working designs away. Those are left to
`Module.check`. -/
private def expectSym (ref : Syntax) (ctx : String) (expected actual : SymWidth) : MacroM Unit :=
  match asNat? expected, asNat? actual with
  | some e, some a => if e != a then throwWidth ref (.widthMismatch ctx e a) else pure ()
  | _, _ => pure ()

/-- Widths that are the same term are the same width; used where a `Bool` is wanted. -/
private def sameWidth (a b : SymWidth) : Bool :=
  match asNat? a, asNat? b with
  | some x, some y => x == y
  | _, _ => toString a.raw == toString b.raw

/-- A port's fields once expanded: the signals it contributes, split by which way they flow.

`here` are the fields that go in the list the port was written in, `there` the ones that go in the
other -- which is how `ready` ends up facing the opposite way from `valid` without being declared
specially. A plain wire has one field and nothing on the far side. -/
structure PortFields where
  here : Array (Ident × SymWidth) := #[]
  there : Array (Ident × SymWidth) := #[]

/-- Expand one interface reference into its fields.

The reference is expanded with `Macro.expandMacro?`, which is what makes interfaces possible as a
macro at all: it gives the field names at macro-expansion time, where the width analysis needs
them. `Macro.Context` carries no environment, so there is no other way to find them. -/
private def expandIface (iface : TSyntax `circuit_iface) :
    MacroM (Array (Bool × Ident × SymWidth)) := do
  let some expanded ← Macro.expandMacro? iface
    | Macro.throwErrorAt iface
        "unknown interface; declare one with a `macro_rules` producing `fields! { .. }`"
  let `(circuit_fields| fields! { $[$fs:circuit_field],* }) := expanded
    | Macro.throwErrorAt iface "interface did not expand to a `fields! { .. }` list"
  fs.mapM fun f => do
    match f with
    -- The field name is a fixed label, not a binder, so its macro scopes are dropped: the body
    -- writes `s.valid` with no scopes and the two have to agree.
    | `(circuit_field| fwd $x:ident : $w:term) =>
        return (true, mkIdent x.getId.eraseMacroScopes, w)
    | `(circuit_field| rev $x:ident : $w:term) =>
        return (false, mkIdent x.getId.eraseMacroScopes, w)
    | _ => Macro.throwErrorAt f "expected `fwd name : width` or `rev name : width`"

/-- Qualify a field with its port: field `valid` of port `s` is the signal `s.valid`. -/
private def qualify (port : Ident) (field : Ident) : Ident :=
  mkIdentFrom port (port.getId ++ field.getId)

/-- Parse a `circuit_port` into the signals it denotes, keeping identifiers for error positions. -/
private def parsePort (p : TSyntax `circuit_port) : MacroM PortFields := do
  match p with
  | `(circuit_port| $x:ident : $w:term) => return { here := #[(x, w)] }
  | `(circuit_port| slave $x:ident : $i:circuit_iface) => do
      let fs ← expandIface i
      -- A slave port is incoming: what flows forward arrives, what flows back departs.
      return { here := fs.filterMap fun (isFwd, f, w) => if isFwd then some (qualify x f, w) else none
               there := fs.filterMap fun (isFwd, f, w) => if isFwd then none else some (qualify x f, w) }
  | `(circuit_port| master $x:ident : $i:circuit_iface) => do
      let fs ← expandIface i
      -- A master port is outgoing, so it is the mirror image.
      return { here := fs.filterMap fun (isFwd, f, w) => if isFwd then some (qualify x f, w) else none
               there := fs.filterMap fun (isFwd, f, w) => if isFwd then none else some (qualify x f, w) }
  | _ => Macro.throwErrorAt p "expected `name : width`, `slave x : iface` or `master x : iface`"

/-- Add a declaration to an environment, rejecting a disagreeing redeclaration. -/
private def addDeclStx (env : List (Name × SymWidth)) (x : Ident) (w : SymWidth) :
    MacroM (List (Name × SymWidth)) :=
  match env.lookup x.getId with
  | some w' =>
      if sameWidth w w' then return env
      else match asNat? w', asNat? w with
        | some a, some b => throwWidth x (.conflictingDecl x.getId a b)
        | _, _ => return env
  | none => return (x.getId, w) :: env

/-- Build an environment from a list of declarations, in source order. -/
private def envOfDecls (decls : Array (Ident × SymWidth)) : MacroM (List (Name × SymWidth)) := do
  let mut env : List (Name × SymWidth) := []
  for (x, w) in decls do
    env ← addDeclStx env x w
  return env

/-- Resolve a name against the environment, reporting an unbound signal at its use site. -/
private def widthOfStx (env : List (Name × SymWidth)) (x : Ident) : MacroM SymWidth :=
  match env.lookup x.getId with
  | some w => return w
  | none => throwWidth x (.unbound x.getId)

/-- If this expression is a bare identifier naming a declared memory, its name and element width.

This is what distinguishes `mem[addr]` from `x[3]`: both parse the same way, and only the
declarations say which was meant. -/
private def memoryName? (ctx : SymCtx) : TSyntax `circuit_expr → Option (Ident × SymWidth)
  | `(circuit_expr| $x:ident) => (ctx.mems.lookup x.getId).map fun (_, w) => (x, w)
  | _ => none

mutual

/-- Synthesise a width bottom-up, returning `none` where the form carries none of its own.

Only a bare literal is genuinely indeterminate; everything else is `none` solely because its
operands are. An unbound identifier is an error here rather than a `none`, since it is an error
however the expression is used. -/
private partial def widthOf? (ctx : SymCtx) (stx : TSyntax `circuit_expr) :
    MacroM (Option SymWidth) := do
  match stx with
  | `(circuit_expr| $x:ident) => return some (← widthOfStx ctx.env x)
  | `(circuit_expr| $_:num) => return none
  | `(circuit_expr| ($e:circuit_expr)) => widthOf? ctx e
  | `(circuit_expr| ($_:circuit_expr : $w:term)) => return some w
  | `(circuit_expr| ~$e:circuit_expr) => widthOf? ctx e
  | `(circuit_expr| $e:circuit_expr << $_:term) => widthOf? ctx e
  | `(circuit_expr| $e:circuit_expr >> $_:term) => widthOf? ctx e
  | `(circuit_expr| $a:circuit_expr + $b:circuit_expr)
  | `(circuit_expr| $a:circuit_expr & $b:circuit_expr)
  | `(circuit_expr| $a:circuit_expr ^ $b:circuit_expr)
  | `(circuit_expr| $a:circuit_expr | $b:circuit_expr) =>
      return (← widthOf? ctx a) <|> (← widthOf? ctx b)
  | `(circuit_expr| $_:circuit_expr == $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr != $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr < $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr <= $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr > $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr >= $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr && $_:circuit_expr)
  | `(circuit_expr| $_:circuit_expr || $_:circuit_expr)
  | `(circuit_expr| !$_:circuit_expr) => return some (← ofNat 1)
  | `(circuit_expr| $_:circuit_expr [ $hi:term : $lo:term ]) => return some (← `($hi - $lo + 1))
  | `(circuit_expr| $e:circuit_expr [ $_:circuit_expr ]) =>
      match memoryName? ctx e with
      | some (_, elemW) => return some elemW
      | none => return some (← ofNat 1)
  | `(circuit_expr| { $es,* }) => do
      let mut total : Option SymWidth := none
      for e in es.getElems do
        match ← widthOf? ctx e with
        | none => return none
        | some w => total := some (← match total with
            | none => pure w
            | some t => `($t + $w))
      return total
  | `(circuit_expr| $_:circuit_expr ? $t:circuit_expr : $f:circuit_expr) =>
      return (← widthOf? ctx t) <|> (← widthOf? ctx f)
  | _ => return none

/-- Lower a comparison. The operand width comes from whichever side determines it.

`negate` covers `!=`; `>` and `>=` are handled by the caller swapping the operands. -/
private partial def checkCmp (ctx : SymCtx) (expected : SymWidth) (ref : Syntax)
    (ctor : TSyntax `term) (negate : Bool) (a b : TSyntax `circuit_expr) :
    MacroM (TSyntax `term) := do
  expectSym ref "result of a comparison" expected (← ofNat 1)
  let some w := (← widthOf? ctx a) <|> (← widthOf? ctx b)
    | throwWidth ref (.indeterminate "the operands of this comparison")
  let cmp ← `(Expr.cmp $ctor $w $(← checkExpr ctx w a) $(← checkExpr ctx w b))
  if negate then `(Expr.not 1 $cmp) else return cmp

/-- Reduce an operand to one bit: `~(e == 0)`, the truth value Verilog's logical operators use. -/
private partial def nonzero (ctx : SymCtx) (e : TSyntax `circuit_expr) : MacroM (TSyntax `term) := do
  let some w ← widthOf? ctx e
    | throwWidth e (.indeterminate "an operand of this logical operator")
  `(Expr.not 1 (Expr.cmp CmpOp.eq $w $(← checkExpr ctx w e) (Expr.lit $w 0)))

/-- Lower a logical `&&` or `||`.

Each operand is reduced to a truth value independently, so the two sides need not have the same
width -- unlike a comparison, which forces one width on both. -/
private partial def checkLogical (ctx : SymCtx) (expected : SymWidth) (ref : Syntax)
    (ctor : TSyntax `term) (a b : TSyntax `circuit_expr) : MacroM (TSyntax `term) := do
  expectSym ref "result of a logical operator" expected (← ofNat 1)
  `(Expr.bin $ctor 1 $(← nonzero ctx a) $(← nonzero ctx b))

/-- Lower an expression, checking it against `expected` and recording that width on every node. -/
private partial def checkExpr (ctx : SymCtx) (expected : SymWidth) (stx : TSyntax `circuit_expr) :
    MacroM (TSyntax `term) := do
  match stx with
  | `(circuit_expr| $x:ident) => do
      let w ← widthOfStx ctx.env x
      expectSym x s!"'{x.getId}'" expected w
      `(Expr.var $(quote x.getId))
  | `(circuit_expr| $lit:num) => do
      let n := lit.getNat
      -- Only checkable when the width is concrete; otherwise `Module.check` catches it.
      if let some e := asNat? expected then
        if n ≥ 2 ^ e then throwWidth lit (.litOverflow e n)
      `(Expr.lit $expected $(quote n))
  | `(circuit_expr| ($e:circuit_expr)) => checkExpr ctx expected e
  | `(circuit_expr| ($e:circuit_expr : $w:term)) => do
      expectSym w "ascribed width" expected w
      checkExpr ctx w e
  | `(circuit_expr| ~$e:circuit_expr) =>
      `(Expr.not $expected $(← checkExpr ctx expected e))
  | `(circuit_expr| !$e:circuit_expr) => do
      expectSym stx "result of a logical operator" expected (← ofNat 1)
      let some w ← widthOf? ctx e
        | throwWidth e (.indeterminate "the operand of this logical negation")
      `(Expr.cmp CmpOp.eq $w $(← checkExpr ctx w e) (Expr.lit $w 0))
  | `(circuit_expr| $a:circuit_expr + $b:circuit_expr) =>
      `(Expr.bin BinOp.add $expected $(← checkExpr ctx expected a) $(← checkExpr ctx expected b))
  | `(circuit_expr| $a:circuit_expr & $b:circuit_expr) =>
      `(Expr.bin BinOp.and $expected $(← checkExpr ctx expected a) $(← checkExpr ctx expected b))
  | `(circuit_expr| $a:circuit_expr | $b:circuit_expr) =>
      `(Expr.bin BinOp.or $expected $(← checkExpr ctx expected a) $(← checkExpr ctx expected b))
  | `(circuit_expr| $a:circuit_expr && $b:circuit_expr) =>
      checkLogical ctx expected stx (← `(BinOp.and)) a b
  | `(circuit_expr| $a:circuit_expr || $b:circuit_expr) =>
      checkLogical ctx expected stx (← `(BinOp.or)) a b
  | `(circuit_expr| $a:circuit_expr ^ $b:circuit_expr) =>
      `(Expr.bin BinOp.xor $expected $(← checkExpr ctx expected a) $(← checkExpr ctx expected b))
  | `(circuit_expr| $e:circuit_expr << $sh:term) =>
      `(Expr.shift ShiftOp.shl $expected $(← checkExpr ctx expected e) $sh)
  | `(circuit_expr| $e:circuit_expr >> $sh:term) =>
      `(Expr.shift ShiftOp.shr $expected $(← checkExpr ctx expected e) $sh)
  | `(circuit_expr| $a:circuit_expr == $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.eq)) false a b
  | `(circuit_expr| $a:circuit_expr != $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.eq)) true a b
  | `(circuit_expr| $a:circuit_expr < $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.lt)) false a b
  | `(circuit_expr| $a:circuit_expr <= $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.le)) false a b
  | `(circuit_expr| $a:circuit_expr > $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.lt)) false b a
  | `(circuit_expr| $a:circuit_expr >= $b:circuit_expr) =>
      checkCmp ctx expected stx (← `(CmpOp.le)) false b a
  | `(circuit_expr| $c:circuit_expr ? $t:circuit_expr : $f:circuit_expr) =>
      `(Expr.mux $(← checkExpr ctx (← ofNat 1) c)
          $(← checkExpr ctx expected t) $(← checkExpr ctx expected f))
  | `(circuit_expr| $e:circuit_expr [ $hi:term : $lo:term ]) => do
      let some we ← widthOf? ctx e
        | throwWidth e (.indeterminate "the operand of this slice")
      -- Bounds are a property of the values, so they are only checkable when all three are
      -- literals; otherwise `Module.check` does it.
      if let (some h, some l, some w) := (asNat? hi, asNat? lo, asNat? we) then
        if !(l ≤ h && h < w) then throwWidth stx (.sliceRange h l w)
      expectSym stx "slice" expected (← `($hi - $lo + 1))
      `(Expr.slice $(← checkExpr ctx we e) $hi $lo)
  -- `e[i]` is a memory read when `e` names a declared memory and a bit select otherwise, which is
  -- how Verilog disambiguates the same spelling.
  | `(circuit_expr| $e:circuit_expr [ $i:circuit_expr ]) => do
      match memoryName? ctx e with
      | some (m, elemW) =>
          expectSym stx s!"element of memory '{m.getId}'" expected elemW
          -- The address may be any width; range is checked when it is evaluated.
          let some aw ← widthOf? ctx i
            | throwWidth i (.indeterminate "the address of this memory read")
          `(Expr.index $(quote m.getId) $elemW $(← checkExpr ctx aw i))
      | none =>
          let `(circuit_expr| $iS:num) := i
            | throwWidth i (.indeterminate "a bit select index, which must be a literal")
          let bit := iS.getNat
          let some we ← widthOf? ctx e
            | throwWidth e (.indeterminate "the operand of this bit select")
          if let some w := asNat? we then
            if bit ≥ w then throwWidth stx (.sliceRange bit bit w)
          expectSym stx "bit select" expected (← ofNat 1)
          `(Expr.slice $(← checkExpr ctx we e) $(quote bit) $(quote bit))
  | `(circuit_expr| { $es,* }) => do
      let elems := es.getElems
      let mut widths : Array SymWidth := #[]
      for e in elems do
        let some w ← widthOf? ctx e
          | throwWidth e (.indeterminate "an operand of this concatenation")
        widths := widths.push w
      let some total := (← widthOf? ctx stx)
        | throwWidth stx (.indeterminate "this concatenation")
      expectSym stx "concatenation" expected total
      let pairs := elems.zip widths
      let some (lastE, lastW) := pairs.back?
        | Macro.throwErrorAt stx "a concatenation needs at least one operand"
      let mut acc ← checkExpr ctx lastW lastE
      for (e, w) in pairs.pop.reverse do
        acc ← `(Expr.concat $(← checkExpr ctx w e) $acc)
      return acc
  | _ => Macro.throwErrorAt stx "unsupported circuit expression"

end

/-- The declared width of an assignment target, rejecting assignment to an input port. -/
private def assignTargetWidth (ctx : SymCtx) (x : Ident) : MacroM SymWidth := do
  if ctx.inputs.contains x.getId then
    throwWidth x (.assignToInput x.getId)
  widthOfStx ctx.env x

mutual

/-- Lower a statement against the declarations in `ctx`. -/
private partial def elabStmt (ctx : SymCtx) : TSyntax `circuit_stmt → MacroM (TSyntax `term)
  | `(circuit_stmt| assign $x:ident = $e:circuit_expr ;) => do
      let w ← assignTargetWidth ctx x
      `(Stmt.assign $(quote x.getId) $(← checkExpr ctx w e))
  | `(circuit_stmt| assign $x:ident : $w:term = $e:circuit_expr ;) => do
      let declared ← assignTargetWidth ctx x
      expectSym w s!"ascription on '{x.getId}'" declared w
      `(Stmt.assign $(quote x.getId) $(← checkExpr ctx declared e))
  | `(circuit_stmt| assign $m:ident [ $addr:circuit_expr ] = $e:circuit_expr ;) => do
      let some (_, elemW) := ctx.mems.lookup m.getId
        | throwWidth m (if (ctx.env.lookup m.getId).isSome then .notAMemory m.getId
                        else .unboundMem m.getId)
      let some aw ← widthOf? ctx addr
        | throwWidth addr (.indeterminate "the address of this memory write")
      `(Stmt.store $(quote m.getId) $elemW
          $(← checkExpr ctx aw addr) $(← checkExpr ctx elemW e))
  | `(circuit_stmt| if $c:circuit_expr { $[$t:circuit_stmt]* } else { $[$f:circuit_stmt]* }) => do
      let cond ← checkExpr ctx (← ofNat 1) c
      `(Stmt.if_ $cond $(← mkStmtList ctx t) $(← mkStmtList ctx f))
  | stx => Macro.throwErrorAt stx "unsupported circuit statement"

/-- Build a `List Stmt` literal from a block of statements. -/
private partial def mkStmtList (ctx : SymCtx) (ss : Array (TSyntax `circuit_stmt)) :
    MacroM (TSyntax `term) := do
  let nil ← `(([] : List Stmt))
  ss.foldrM (init := nil) fun s acc => do `($(← elabStmt ctx s) :: $acc)

end

/-- A clock must name a declared 1-bit signal. -/
private def checkClock (ctx : SymCtx) (c : Ident) : MacroM Unit := do
  let w ← widthOfStx ctx.env c
  expectSym c s!"clock '{c.getId}'" (← ofNat 1) w

/-- Lower a module body item. -/
private def elabItem (ctx : SymCtx) : TSyntax `circuit_item → MacroM (TSyntax `term)
  | `(circuit_item| reg $x:ident : $w:term ;) => `(Item.reg $(quote x.getId) $w)
  | `(circuit_item| reg $x:ident [ $d:term ] : $w:term ;) =>
      `(Item.mem $(quote x.getId) $d $w)
  | `(circuit_item| wire $x:ident : $w:term ;) => `(Item.reg $(quote x.getId) $w)
  | `(circuit_item| ( $sub:term ) $iname:ident ( $[$bs:circuit_binding],* ) ;)
  | `(circuit_item| $sub:ident $iname:ident ( $[$bs:circuit_binding],* ) ;) => do
      let nil ← `(([] : List (Name × Name)))
      let bindings ← bs.foldrM (init := nil) fun b acc => do
        let `(circuit_binding| . $port:ident ( $sig:ident )) := b
          | Macro.throwErrorAt b "expected a port binding of the form `.port(signal)`"
        `(($(quote port.getId), $(quote sig.getId)) :: $acc)
      -- `$sub` is spliced as the user wrote it, so it resolves at the use site. The macro has no
      -- way to inspect it: the port list is only available later, in `Module.ofItems`.
      `(Item.inst $(quote iname.getId) $sub $bindings)
  | `(circuit_item| always @(posedge $c:ident ) { $[$ss:circuit_stmt]* }) => do
      checkClock ctx c
      `(Item.always (Event.posedge $(quote c.getId)) $(← mkStmtList ctx ss))
  | `(circuit_item| always @(negedge $c:ident ) { $[$ss:circuit_stmt]* }) => do
      checkClock ctx c
      `(Item.always (Event.negedge $(quote c.getId)) $(← mkStmtList ctx ss))
  | `(circuit_item| $s:circuit_stmt) => do
      `(Item.stmt $(← elabStmt ctx s))
  | stx => Macro.throwErrorAt stx "unsupported circuit item"

/-- Build a `List (Name × Nat)` literal from a parsed port list. -/
private def mkPortList (ps : Array (Ident × SymWidth)) : MacroM (TSyntax `term) := do
  let nil ← `(([] : List (Name × Nat)))
  ps.foldrM (init := nil) fun (x, w) acc => `(($(quote x.getId), $w) :: $acc)

/-- Build a `List Item` literal from a parsed module body.

Recursive rather than a fold because a `let` item wraps everything after it. -/
private partial def mkItemList (ctx : SymCtx) : List (TSyntax `circuit_item) →
    MacroM (TSyntax `term)
  | [] => `(([] : List Item))
  | i :: rest => do
      match i with
      | `(circuit_item| let $x:ident := $v:term ;) =>
          `(let $x := $v; $(← mkItemList ctx rest))
      | _ => `($(← elabItem ctx i) :: $(← mkItemList ctx rest))

/-- The `reg` and `wire` declarations in a module body, which join the ports in the environment. -/
private def regDecls (is : Array (TSyntax `circuit_item)) : MacroM (Array (Ident × SymWidth)) := do
  let mut regs : Array (Ident × SymWidth) := #[]
  for i in is do
    if let `(circuit_item| reg $x:ident : $w:term ;) := i then
      regs := regs.push (x, w)
    if let `(circuit_item| wire $x:ident : $w:term ;) := i then
      regs := regs.push (x, w)
  return regs

/-- The memory declarations in a module body. -/
private def memDecls (is : Array (TSyntax `circuit_item)) :
    MacroM (Array (Ident × SymWidth × SymWidth)) := do
  let mut mems : Array (Ident × SymWidth × SymWidth) := #[]
  for i in is do
    if let `(circuit_item| reg $x:ident [ $d:term ] : $w:term ;) := i then
      mems := mems.push (x, d, w)
  return mems

macro_rules
  | `([circuit_expr ( $[$ps:circuit_port],* ) | $e:circuit_expr : $expected:term ]) => do
      let env ← envOfDecls ((← ps.mapM parsePort).flatMap (·.here))
      checkExpr { env := env } expected e

/-- Lower a whole module to the `Module.ofItems` term that builds it.

Shared by the term form and the declaration form below, which differ only in what they do with the
result. -/
private def buildModule (name : Ident) (ins outs : Array (TSyntax `circuit_port))
    (items : Array (TSyntax `circuit_item)) : MacroM (TSyntax `term) := do
  let insF ← ins.mapM parsePort
  let outsF ← outs.mapM parsePort
  -- A port contributes its forward fields to the list it was written in and its reverse fields to
  -- the other, so a `slave`'s `ready` becomes an output without being declared as one.
  let insP := insF.flatMap (·.here) ++ outsF.flatMap (·.there)
  let outsP := outsF.flatMap (·.here) ++ insF.flatMap (·.there)
  let regsP ← regDecls items
  let env ← envOfDecls (insP ++ outsP ++ regsP)
  let memsP ← memDecls items
  let ctx : SymCtx :=
    { env := env
      inputs := (insP.map (·.1.getId)).toList
      mems := (memsP.map (fun (x, d, w) => (x.getId, d, w))).toList }
  `(Module.ofItems $(quote name.getId)
      $(← mkPortList insP) $(← mkPortList outsP) $(← mkItemList ctx items.toList))

macro_rules
  | `(module! $name:ident
        ( $[$ins:circuit_port],* ) -> ( $[$outs:circuit_port],* )
        { $[$items:circuit_item]* }) => buildModule name ins outs items

/-- A module as a **declaration**, with parameters, in the shape Verilog writes one.

```lean
module! counter #[(w : Nat)] (clk : 1) -> (q : w) {
  reg q : w;
  always @(posedge clk) { assign q = q + 1; }
}
```

declares `counter : Nat → Module`. The parameters are ordinary Lean binders, so everything a binder
can do works here -- types, implicits, instance arguments, and defaults including `:= by decide`,
which is how a module states a precondition on its own parameters.

`#[…]` is required, even when empty. Without a marker there is no way to tell a parameter list from
a port list: `(w : Nat)` and `(clk : 1)` have the same shape. Verilog solved this the same way. -/
syntax (docComment)? "module!" ident "#[" bracketedBinder* "]"
  "(" sepBy(circuit_port, ",") ")" "->" "(" sepBy(circuit_port, ",") ")"
  "{" circuit_item* "}" : command

macro_rules
  | `($[$doc:docComment]? module! $name:ident #[ $bs:bracketedBinder* ]
        ( $[$ins:circuit_port],* ) -> ( $[$outs:circuit_port],* )
        { $[$items:circuit_item]* }) => do
      `($[$doc:docComment]? def $name $bs* : Module := $(← buildModule name ins outs items))

end CircuitHdl
