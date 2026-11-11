# vc

## Embedded Verilog subset

`Vc.Basic` uses Lean `syntax` and `macro_rules` to translate single-bit
combinational Verilog expressions into `Wire := Nat -> Bool` values.

```lean
import Vc.Basic

def inputA : Wire := fun time => time % 2 == 1
def inputB : Wire := fun time => time % 4 >= 2

verilog assign sum = inputA ^ inputB;
verilog assign carry = inputA & inputB;

#eval visualize sum 0 3
#eval visualize (verilog[ ~inputA | inputB ]) 0 3
```

Supported expressions: Lean identifiers bound to wires, constants `0` and `1`,
parentheses, unary `!` and `~`, and binary `&`, `^`, and `|` (in descending
precedence). Binary operators associate to the left. Both unary operators
negate a single bit. A prefixed `verilog assign name = expression;` command
defines a Lean wire; `verilog[ expression ]` can be used directly as a term.

This is an embedded DSL, not a parser for external `.v` files. It does not yet
support modules, port declarations, vectors, four-state logic, arithmetic,
feedback, or `always` blocks. Wire references must already exist in Lean scope.

Run the waveform, precedence, diagnostic, and proof checks from this directory:

```console
lake env lean Vc/Basic.lean
lake env lean tests/file_verilog.lean
lake build
```