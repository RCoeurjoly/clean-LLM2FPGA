# CircuitHdl

A Lean-embedded circuit HDL: a typed core for digital hardware, a Verilog-flavoured surface
syntax that lowers into it, and an executable semantics you can simulate.

The language itself depends on nothing but core `Lean` and `BitVec` — no Mathlib — so it builds
against a bare toolchain in well under a minute, with no build cache. Please keep it that way.

There is exactly one dependency, and exactly one module that may use it: `CircuitHdl.Circt`
lowers a `Module` to the CIRCT dialects through [VeIR](https://github.com/opencompl/veir), a Lean
formalisation of MLIR, which then *verifies* the result. Nothing else in the package may import
it. VeIR is a path dependency on a sibling `../veir` checkout, so that checkout's state is part
of this build.

## Goals

- Model basic circuit concepts as a typed Lean core.
- Separate syntax, elaboration, and semantic interpretation.
- Make the language extensible with Lean `syntax`/`macro_rules` patterns.
- Keep sequential and combinational logic distinct in the core AST.

## Layout

The language itself, under `CircuitHdl/`:

| File | Contents |
|---|---|
| `Core.lean` | The typed AST (`Expr`, `Stmt`, `Event`, `Process`, `Module`) and module assembly (`Item`, `Module.ofItems`). |
| `Width.lean` | The width rules as ordinary Lean over the AST (`Expr.width?`, `Module.check`). |
| `Clocks.lean` | Clock-domain analysis (`Module.clockDomains`, `Module.singleClocked`). |
| `Syntax.lean` | The `module!` surface syntax, its lowering into the AST, and width inference. |
| `Semantics.lean` | Evaluation over core `BitVec` (`Expr.eval`, `Module.step`, `Module.run`). |
| `Transactions.lean` | Driving AXI-Stream ports at transaction level (`Module.transfer`). |
| `Circt.lean` | Lowering to the CIRCT dialects `hw`/`comb`/`seq` via VeIR (`Module.toMlir`, `Module.toVeir`). The one module with a dependency outside core `Lean`. |
| `../CircuitHdl.lean` | Umbrella module importing the above. |

Example designs, one per file, each written in the `module!` surface syntax exactly as a real
design would be — there are no hand-built `Module` literals here:

| File | Contents |
|---|---|
| `Examples/And2.lean` | A two-input AND gate: combinational, one loose `assign`. |
| `Examples/Counter.lean` | A counter with reset: one register, and every width inferred from `reg q : 8;`. |
| `Examples/Alu.lean` | Add / and / or / compare, selected by a mux chain. The example that needs the whole expression language. |
| `Examples/Shifter.lean` | Nibble selection and rotation: slices and concatenation. |
| `Examples/Swap.lean` | `swapSeq` and `swapComb` — identical statements, clocked and unclocked. |
| `Examples/AsyncFifo.lean` | An asynchronous FIFO with AXI valid/ready ports: two clocks, gray-coded pointers, two-flop synchronizers. |
| `Examples/ShiftFifo.lean` | The same interface built the opposite way: one clock, no memory, data moves instead of pointers. |

Alongside the designs, two *specifications* — what a FIFO is, with no reference to any
implementation. Neither imports the HDL, which is what lets both FIFOs above be checked against
the same models:

| File | Contents |
|---|---|
| `Examples/AbstractFifoModel.lean` | A depth and a count. Forgetting the data is what makes the flag logic provable. |
| `Examples/AbstractQueueModel.lean` | A depth and a list, plus `counting`, the map down to the model above. Says the first-in-first-out property, which a counter cannot. |

Tests live in their own library under `CircuitHdlTest/`, so they can be built and run on their
own with `lake test`. Small modules that probe one rule each live with the tests that use them,
since they are assertions rather than designs:

| File | Contents |
|---|---|
| `CircuitHdlTest/Syntax.lean` | Expression lowering and precedence, plus round-trips asserting the example designs lower to the hand-written reference structures (`and2Example`, `counterExample`) kept here. |
| `CircuitHdlTest/Semantics.lean` | Executable tests: the counter counts, the and2 truth table, non-blocking assignment. |
| `CircuitHdlTest/Clocks.lean` | Clock-domain analysis on small modules: what it accepts, rejects, and where the boundary sits. |
| `CircuitHdlTest/AsyncFifo.lean` | The FIFO simulated at three clock ratios and with intermittent handshakes, gray-code and synchronizer unit tests, and its refinement of the abstract models — including `bv_decide` proofs that the gray-coded full and empty flags fire at exactly the right occupancy. |
| `CircuitHdlTest/ShiftFifo.lean` | The shift-register FIFO: that it carries the same traffic, and that it refines the same models by entirely different means. |
| `CircuitHdlTest/AbstractFifoModel.lean`, `AbstractQueueModel.lean` | The two specifications at their edges: saturation, overflow, degenerate depths. |
| `CircuitHdlTest/FifoHarness.lean` | Shared machinery for checking any design against the models. |
| `CircuitHdlTest/Circt.lean` | The CIRCT lowering: the text it emits for the smallest designs, and VeIR's verdict on every example. |

## Layer 2: typed circuit core

The typed core covers:

- width-aware expressions (`Expr`)
- assignment and conditional statements (`Stmt`)
- process blocks with sensitivity (`Process`)
- module skeletons with input/output/reg declarations (`Module`)
- explicit event kinds (`posedge`, `negedge`)

## Layer 3: surface syntax

`module!` parses a module declaration and lowers it to the same typed `Module` AST. Each surface
form has its own syntax category (`circuit_port`, `circuit_expr`, `circuit_stmt`, `circuit_item`)
and is lowered through a bracketed term entry point, following the pattern used by the MLIR EDSL in
[`LeanMLIR/MLIRSyntax/GenericParser.lean`](../LeanMLIR/LeanMLIR/MLIRSyntax/GenericParser.lean)
(a pattern borrowed from the neighbouring package, not a dependency on it).

> Keying the `macro_rules` on bracketed forms such as `[circuit_expr (q : 8) | q + 1 : 8]` matters. An earlier
> draft of this sketch wrote the rules against the `term` category directly — ``macro_rules | `($x:ident) => `(Expr.var …)`` —
> which rewrites *every* identifier in the file, including the `Expr.var` it generates, and loops
> until the elaborator hits its recursion limit. A dedicated category cannot do that.

A module body may mix register declarations, clocked blocks, and loose statements. Loose statements
are gathered into a single combinational process; each `always` block becomes its own sequential
process. That partitioning lives in `Module.ofItems` as ordinary Lean rather than in the macro, so
it stays readable and testable on its own.

### Operators

Loosest to tightest, following C:

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
| max | ident, num, `( e )`, `( e : w )`, `{ e, … }`, `e[hi:lo]`, `e[i]` |

As in Verilog, `&` `|` `^` `~` are **bitwise** and `&&` `||` `!` are **logical** — the latter reduce
each operand to a truth value and yield one bit. So `12 & 5` is `4`, bit by bit, while `12 && 5` is
`1`. Each operand of a logical operator is reduced independently, so the two sides need not have the
same width.

Note that Verilog puts the logical operators *looser* than the bitwise ones, where C puts `&&`
between `^` and `==`; `a || b | c` groups differently in the two languages.

Comparisons are unsigned (core `BitVec.ult`/`ule`); `BitVec.slt`/`sle` are there if signed variants
are wanted later.

Several forms never reach the AST — the macro desugars them into nodes that already exist, so
`Core` needs no constructor for any of them:

```
a != b  ==>  ~(a == b)            a > b   ==>  b < a
!a      ==>  a == 0               a && b  ==>  ~(a == 0) & ~(b == 0)
```

`{a, b, c}` folds right into binary `Expr.concat`, leftmost operand in the high bits, matching
`BitVec.append`. `x[i]` is `x[i:i]`.

`always @(posedge clk)` is spelled as in Verilog, but `@(posedge` is lexed as a single token, so a
space between `@(` and `posedge` will not parse. That is deliberate: splitting it would make
`posedge` a reserved word across the whole file and break `Event.posedge`.

## Layer 4: width analysis

Declarations are the single source of truth for widths. The target of an assignment fixes the
expected width, which is pushed down through the expression — so `reg q : 8;` is what makes
`assign q = q + 1;` produce `Expr.add 8 (Expr.var `q) (Expr.lit 8 1)`, with the literal taking its
width from the context rather than defaulting.

`assign x : w = e` pins a width explicitly. The ascription is itself checked against the
declaration, so it can only restate it; it exists to make a width visible at the point of use, not
to override the analysis.

The rules are enforced in two places:

- **At macro-expansion time** (`Syntax.lean`), which is what infers the widths and reports errors
  positioned on the offending token. Nothing is checked at runtime.
- **Over the AST** (`Width.lean`), as ordinary Lean: `Expr.width?` and `Module.check` return
  `Except CheckError _`. This is what validates a module built by hand, such as the reference
  modules in `CircuitHdlTest/Syntax.lean`.

Both share `CheckError`, so their messages agree, and the test file asserts that modules accepted by
the macro also pass `Module.check` — which is what keeps the two from drifting apart.

Beyond width equality, the analysis rejects a literal too large for its width, a reference to an
undeclared signal, an assignment whose target is an input port, a name declared twice with
disagreeing widths, and a clock that is not a declared 1-bit signal. A name may legitimately be
declared more than once — in `counterExample`, `q` is both an output and a reg — provided the widths
agree.

```
width mismatch in 'q': expected 1 bit, got 8 bits
literal 5 does not fit in 1 bit
cannot assign to 'a': it is an input port
```

## Layer 5: semantics

`Semantics.lean` evaluates the AST over core `BitVec`. Because the AST is untyped — widths are
`Nat` fields rather than type indices — a runtime `Value` carries its own width, and evaluation
returns `Except EvalError`. The error types are split by phase: `CheckError` is what the static
analysis and the macro report, and `EvalError` wraps it for the simulator, adding the one failure
that is genuinely dynamic -- a memory index out of range, which depends on the value an address
evaluates to.

### Blocking versus non-blocking

This is where the `ProcKind` split in the AST stops being cosmetic:

- A **combinational** process runs its statements in order, each assignment immediately visible to
  the statements after it.
- A **sequential** process uses Verilog's non-blocking semantics. Every right-hand side — and every
  `if` condition — is evaluated against the state as it was at the clock edge, and all updates land
  together at the end.

So the same two statements mean different things in the two kinds of block:

```lean
always @(posedge clk) { assign a = b; assign b = a; }   -- swaps a and b
assign a = b; assign b = a;                            -- both end up holding b's old value
```

The pair is `Examples/Swap.lean`, and both halves are run in `CircuitHdlTest/Semantics.lean`; the
contrast is what pins the semantics down.

### Running a module

`Module.step` is one clock tick: settle the combinational processes, take the clock edge, settle
again. Settling on both sides is what a simulator does, and it is what makes the returned state
consistent — the outputs reflect the registers as they are *after* the tick. `Module.run` folds
`step` over a list of input vectors, one per tick.

```lean
#guard counterTrace (List.replicate 4 tick) = some [1, 2, 3, 4]
```

These are real executions — `#guard` forces evaluation at compile time — so a green build means the
counter genuinely counts.

### Modules as declarations

`module!` has two forms. As a **term** it builds a `Module` you can assign or pass around, which is
what lets `(grayOfW (n+1)) wg (.b(…))` instantiate one inline. As a **command** it declares the
definition itself, with the parameters on the module rather than on a `def` wrapped around it:

```lean
module! counter #[(w : Nat)] (clk : 1) -> (q : w) {
  reg q : w;
  always @(posedge clk) { assign q = q + 1; }
}
```

declares `counter : Nat → Module`. Parameters are ordinary Lean binders, so types, implicits,
instance arguments and defaults all work — including `:= by decide`, which is how a module states a
precondition on its own parameters.

`#[…]` is required even when empty. `(w : Nat)` and `(clk : 1)` have the same shape, so without a
marker a parameter list and a port list cannot be told apart; Verilog solved this the same way
with `#(…)`.

A width computed from the parameters is bound by a **`let` item**, which scopes over the items after
it:

```lean
let n := Nat.log2 depth;
reg wbin : n+1;
```

A defaulted parameter `(n : Nat := Nat.log2 depth)` would have worked too, but a caller could
override it into disagreeing with the `depth` it was derived from. A `let` cannot be overridden.

### Parameterized widths

Width positions take Lean **terms**, so a module can be a function of its size:

```lean
def grayOfW (w : Nat) : Module :=
  module! grayOfW (b : w) -> (g : w) { assign g = b ^ (b >> 1); }
```

Two widths can only be compared at macro time when the comparison is decidable there: concrete
widths compare numerically — which is why a fixed-size module still reports every error it used to —
and two symbolic widths compare by syntax. Anything else is deferred to `Module.check`, where the
widths are ordinary `Nat`s.

So parameterizing does not weaken checking so much as move it. `(m n).check = .ok ()` is a
proposition about *every* size, which is a stronger thing to know than a macro checking one — see
the well-formedness section of `CircuitHdlTest/AsyncFifo.lean` for how far that currently goes.

Two grammar positions had to be tightened when widths became terms, because `term` is greedy:
`assign x : w = e;` would have parsed `w = e` as an equation, and `a << 1 < b` would have parsed
`1 < b` as the shift amount. The first takes a width above `=`'s precedence, the second only an
atom.

### Why lowering is bidirectional

Pushing the expected width down is not enough on its own, because three forms do not preserve
width: a comparison is always one bit, a concatenation is the sum of its operands, and a slice is
fixed by its bounds. For those, the expected width says nothing about the operands. So `Syntax.lean`
runs two passes — `widthOf?` synthesises a width bottom-up, and `checkExpr` pushes one down, calling
`widthOf?` wherever it needs an operand width it cannot otherwise know.

A bare literal has no width of its own, so `widthOf?` returns `none` for it. That only matters where
nothing else determines the width: in `q < 5` the literal takes its width from `q`, but `{3, a}` is
rejected — exactly as Verilog rejects an unsized literal in a concatenation. The ascription
`(3 : 4)` supplies one.

```
cannot infer a width for an operand of this concatenation; give it one with an ascription, as in (3 : 8)
width mismatch in result of a comparison: expected 8 bits, got 1 bit
slice [9:2] is out of range for a 8-bit value
```

### Memories

```
reg mem[4] : 8;          -- 4 words of 8 bits
mem[addr]                -- read
assign mem[addr] = d;    -- write
```

`mem[addr]` and the bit select `x[3]` are spelled identically, so **the declarations decide which is
meant** — exactly as in Verilog. A bit select still requires a literal index; a memory address can be
any expression, of any width.

Whether an address is in range depends on the value it evaluates to, so it is checked at evaluation
rather than statically, and an out-of-range access is an error rather than wrapping — a pointer bug
should surface, not silently alias. This is the one failure `EvalError` adds over `CheckError`.

`wire x : w;` is accepted as a **synonym for `reg`**. It reads better for the intermediate
combinational signals a design accumulates, but the AST does not distinguish the two; this is a
surface alias, not real `wire` support.

### Interfaces

An AXI port is one thing, but spelling it as three loose wires records neither that they belong
together nor that `ready` runs *against* the other two. An interface says both:

```lean
macro_rules
  | `(circuit_iface| axis $dw:term) =>
      `(circuit_fields| fields! { fwd valid : 1, rev ready : 1, fwd data : $dw })

module! asyncFifo #[…] (wclk : 1, rclk : 1, slave s : axis dw) -> (master m : axis dw) { … }
```

A `slave` port is incoming, so its `fwd` fields are inputs and its `rev` fields outputs; a `master`
port is the mirror. That is what makes `ready` face the opposite way without being declared
specially. Fields become dotted names — `s.valid`, `s.ready`, `s.data` — and the body writes those
directly.

Bundling is **pure declaration sugar**: a bundled module and the equivalent flat one are literally
the same `Module`, which `CircuitHdlTest.Syntax` asserts with `rfl`. Nothing downstream knows about
interfaces, because nothing in the lowering ever decomposes a name — every identifier is an opaque
`Name`, so a dotted one flows through the width analysis and semantics unchanged.

Two things worth knowing:

- **Interfaces are declared with `macro_rules`, not a dedicated `interface!` command.** That is a
  limitation rather than a preference: generating this from a command would mean splicing the
  interface's name into the *pattern* of a nested quotation, and every antiquotation inside
  `` `(macro_rules | `(cat| …) => …) `` belongs to the generated code, so the outer name cannot
  reach it. Sugar for this needs a command elaborator building the syntax by hand.
- `module!` finds an interface's fields with `Macro.expandMacro?`, which is what makes this possible
  as a macro at all — the field names are needed at macro-expansion time, where the width analysis
  runs, and `Macro.Context` carries no environment.

### Transaction-level driving

A testbench should say what to send, not wiggle wires:

```lean
#guard (fifo4.transfer sPort mPort [1, 2, 3, 4, 5, 6] (srcRate := 3)).toOption
  = some [1, 2, 3, 4, 5, 6]
```

`Module.transfer` holds an offer until it is taken (the AXI rule that `valid` may not be withdrawn),
accepts when it can, picks which clock to tick, and reports what transferred. `srcRate`/`dstRate`
run the two domains at different speeds.

It also absorbs two things every scenario previously had to restate: the flags must settle before
`valid` means anything, since the design has no reset; and the run must stop once everything offered
has arrived, because accepting past that point re-reads a drained FIFO whose pointers have wrapped.
Both were found by the driver returning duplicated and invented data until they were handled.

### Module instantiation

```
grayOf wg (.b(wbnext), .g(wgnext));
```

An instance is **flattened at assembly**: a bound port becomes the parent signal it is tied to, and
anything unbound becomes an instance-prefixed local (`wg.q1`), so two instances of the same module
cannot collide. The width rules and the semantics therefore know nothing about instances — by the
time they are handed a `Module`, there are none left. A submodule's clock is a signal like any
other, so binding it renames the sensitivity list too.

Port bindings are **plain signal names, not expressions**. `Macro.Context` carries no environment,
so at macro-expansion time the lowering cannot look up the submodule's port list — it cannot tell an
input port from an output, and so cannot know which side to lower as an expression. Binding by name
keeps both directions uniform, and the submodule is simply spliced through as a Lean term and
resolved later, where the `Module` value is in hand. To feed an expression, assign it to a `wire`
first.

Flattening erases the connection, so the bindings are kept on the side in `Module.insts` and checked
separately. Without that a mistyped port name would silently disconnect a signal — the real port
would just become an unbound local — and a width mismatch would be blamed on an inlined operand:

```
instance 'a' binds 'nope', which is not a port of the module it instantiates
width mismatch in port 'b' of instance 'a': expected 3 bits, got 4 bits
```

### Independent clock domains

`Module.step` fires *every* sequential process, which is only right for a single-clock design. For
two clocks, `Module.stepClock` takes an edge on one named clock and leaves the other domain's
registers alone, and `Module.runClocks` drives a list of `(clock, inputs)` pairs so a testbench can
interleave domains at any ratio:

```lean
two.runClocks two.initState [(`ca, []), (`ca, []), (`cb, []), (`ca, [])]
-- qa: 1 2 2 3     qb: 0 0 1 1
```

### Not yet covered

There is no division, multiplication, or
variable-amount shifts.

## Example 1: 2-input AND

```lean
module! and2 #[] (a : 1, b : 1) -> (y : 1) {
  assign y = a & b;
}
```

equivalent to:

```verilog
module and2(input a, b, output y);
  assign y = a & b;
endmodule
```

## Example 2: counter with reset

```lean
module! counter #[] (clk : 1, rst : 1) -> (q : 8) {
  reg q : 8;
  always @(posedge clk) {
    if rst { assign q = 0; }
    else   { assign q = q + 1; }
  }
}
```

equivalent to:

```verilog
module counter(
  input clk,
  input rst,
  output reg [7:0] q
);
  always @(posedge clk) begin
    if (rst)
      q <= 8'd0;
    else
      q <= q + 8'd1;
  end
endmodule
```

Both designs live in `Examples/`, written in the surface syntax above, and are checked against
hand-written reference values kept in `CircuitHdlTest/Syntax.lean`:

```lean
example : and2 = and2Example := rfl
```

## Example 3: an ALU

Needs the mux chain, a comparison, and the bitwise ops together:

```lean
module! alu #[] (op : 2, a : 8, b : 8) -> (y : 8) {
  assign y = op == 0 ? a + b
           : op == 1 ? a & b
           : op == 2 ? a | b
           : {(0 : 7), a < b};
}
```

The `op == 0` comparisons get their operand width from `op`, and the final arm pads a one-bit
comparison out to eight with a concatenation.

## Example 4: an asynchronous FIFO with AXI handshakes

`Examples/AsyncFifo.lean` is the design that motivated memories and independent clocks: two clocks that
share nothing but a memory and a pair of gray-coded pointers. It is **parameterized by size** —
`asyncFifo depth dataWidth`, where the depth must be a power of two and at least 4, and that is
*enforced* rather than assumed. The ports are
AXI-style — an AXI
slave port `s_valid`/`s_ready`/`s_data` on `wclk`, an AXI master port `m_valid`/`m_ready`/`m_data`
on `rclk` — so a transfer happens on an edge where both halves of a handshake are high.

```lean
def validDepth (d : Nat) : Bool := 4 ≤ d && d &&& (d - 1) == 0

module! asyncFifo #[(depth dw : Nat) (_h : validDepth depth = true := by decide)]
    (wclk : 1, s_valid : 1, s_data : dw, rclk : 1, m_ready : 1)
 -> (s_ready : 1, m_valid : 1, m_data : dw) {
    let n := Nat.log2 depth;
    reg mem[depth] : dw;
    reg wbin : n+1;
    …
    assign waddr = wbin[n-1:0];
  }
```

Widths are Lean **terms**, not numerals, so a module can be a function of its size — which is what
lets a property be *stated* about every size.

The memory holds `depth` words while every internal width derives from `Nat.log2 depth`, so the two
only agree when `depth` is a power of two. That is a real precondition, and carrying the proof makes
it one the compiler checks: the `by decide` default fills itself in for a good size, and a bad one
simply does not elaborate.

```
asyncFifo 5 8
-- error: Tactic `decide` proved that the proposition
--   validDepth 5 = true
-- is false
```

The floor of 4 is the other half of the contract: the full test compares the top *two* pointer bits,
so a shallower design has nothing to compare.

The two gray encoders are instances of `grayOf` rather than open-coded expressions, one per clock
domain. Every assertion in the testbench is unchanged from when they were written out inline, which
is the check that instantiating a module and inlining its logic come to the same thing.

Gray code is the point of the crossing. A binary pointer sampled by the other clock domain can be
caught mid-transition with several bits in flight, yielding a value it never actually held. A gray
pointer changes exactly one bit per increment, so the worst a sampler sees is the value just before
or just after — both safe. Each pointer then crosses a two-flop synchronizer, and the flags compare
the local pointer against the synchronized remote one.

`s_ready` is `~full` and `m_valid` is `~empty`, and both `full` and `empty` are **registers**, so
neither ready signal depends combinationally on the valid facing it — which is what stops two such
FIFOs from forming a loop when connected back to back.

The handshake makes the central property a single equation: whatever the clock ratio, the bytes
completing read handshakes are exactly those that completed write handshakes.

```lean
#guard go (settleT ++ (List.map (fun d => wTick d 1) [1, 2, 3, 4, 5, 6])
                   ++ List.replicate 10 (rTick 1))
  = some ([1, 2, 3, 4], [1, 2, 3, 4], true, true)
```

Six beats offered to a depth-4 FIFO: back-pressure means only four handshakes complete, so two
beats are *held off* rather than dropped — which is the real gain over a bare increment bit, where
an increment asserted while full was simply lost.

One result is worth reading twice. In the 1:1 case, where the reader keeps up perfectly, `s_ready`
still drops. That is correct, not a bug: the read pointer reaches the write domain two `wclk` ticks
late, so the write side works from a stale view and back-pressures a write it could in principle
have taken. Ready built on synchronized pointers is necessarily pessimistic — and pessimistic is
the safe direction, since the opposite error overwrites data the reader has not taken.

## Clock domains

A register belongs to the clock that drives it. Two clocks driving one register is a race —
whichever edge lands second wins, and across independent clocks the two have no defined order at
all. `Clocks.lean` makes that statable:

```lean
#guard fifo4.clockDomains =
  [(`wbin, [`wclk]), (`wptr, [`wclk]), …, (`rbin, [`rclk]), …, (`waddr, [])]
```

which is the FIFO's whole reason for existing, read back out of the design: the `w` registers on
`wclk`, the `r` registers on `rclk`, and the combinational wires on neither.

And unlike well-formedness, this one is **proved for every size**:

```lean
theorem asyncFifo_singleClocked (depth dw : Nat) (h : validDepth depth = true) :
    (asyncFifo depth dw h).singleClocked = true := by
  unfold asyncFifo
  generalize Nat.log2 depth = n
  rfl
```

The reason it goes through where `Module.check` does not is that none of it reads a width — it is
all names and process kinds — so nothing gets stuck on a symbolic `Nat`.

The `generalize` abstracts the derived width away before `rfl` runs, which states what the proof
relies on: that the property never reads a width. It was worth minutes when `asyncFifo` was a `def`
wrapping a `let`; now that the module is a declaration and the `let` sits inside the body, it is
worth almost nothing.

## Why this is a useful boundary

At this stage, the design already captures the important distinction between:

- combinational logic: "purely derived from current inputs"
- sequential logic: "state updated on clock events"

That distinction is exactly the place where a hardware DSL should become explicit. Lean enforces it
structurally in the AST rather than through ad hoc parser checks, and — now that the semantics are
in — the two kinds of process genuinely behave differently when run.

## Building

From this directory:

```sh
lake build        # the library
lake test         # the library and its test suite
```

`lake build` builds the library **only** — `defaultTargets` does not include the test library, so
a broken assertion there will not show up. Use `lake test`.

The one thing to fetch is the sibling `../veir` checkout that `lakefile.toml` names, pinned by
`lean-toolchain` to the toolchain VeIR uses. Everything else builds in seconds against a bare
toolchain with no cache.

All the tests are checked by the compiler, so a green build means the parser produces exactly the
reference modules (`example … := rfl`), the widths agree (`#guard_msgs`), and the circuits actually
run (`#guard` forces evaluation). There is no separate runner: building the test library *is*
running the tests.

## Next steps

1. ~~a real parser for `module`, `wire`, `reg`, and `always` syntax~~ — done, except `wire`
2. ~~width checking during elaboration~~ — done
3. ~~a small denotational semantics for expressions and procedural blocks, over core `BitVec`~~ — done
4. ~~lowering to an MLIR/SSA-style internal form, with the CIRCT dialects the natural target~~ —
   done, via VeIR; every example design round-trips through `firtool` to Verilog

What is left is mostly about reach rather than new layers:

- **Width checking for every size.** `Module.check` can be proved for a *concrete* FIFO but not
  for all depths at once: `rfl` sticks on `expectWidth` at a symbolic `Nat`, and `simp` will not
  evaluate `Name` equality inside a `match` scrutinee. The fix is for `Module.check` to take an
  environment rather than compute one. The obstacle is written up at the end of
  `CircuitHdlTest/AsyncFifo.lean`.
- **Unbounded refinement.** The FIFO refinement results are bounded enumerations over schedules,
  because symbolically evaluating `Module.settle` runs into the same wall. The models' own
  properties, and the gray-coded flag logic, *are* proved outright.
- **A semantics that can see metastability.** Evaluation is deterministic, so the reason the
  asynchronous FIFO's pointers are gray coded — that a sampler catching one mid-transition reads
  only its old or new value — is invisible to simulation at any horizon.
