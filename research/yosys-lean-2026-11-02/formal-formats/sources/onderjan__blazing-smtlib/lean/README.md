# BSmtLib: Fast, safe, dependency-free SMT-LIB parsing in Lean

A high-performance strict parsing library for [SMT-LIB 2](https://smt-lib.org/)
with no dependencies other than the Lean standard library and no unsafe code.
SMT-LIB versions 2.0, 2.5, 2.6, and 2.7 are supported, with 2.0 disabled by
default due to its deprecated status.

**BSmtLib** is a port of the Rust crate **blazing-smtlib** to Lean 4, with the
same architecture and public interface, adapted to Lean where needed. Both are
in the [library repository](https://github.com/onderjan/blazing-smtlib).

**BSmtLib** is designed for composability and, in addition to reading
SMT-LIB scripts, supports reading and writing of e.g. bare SMT-LIB terms,
commands, or full SMT-LIB solver responses using custom parsers.

## Installation

Add **BSmtLib** to the `lakefile.toml` of your package:

```toml
[[require]]
name = "bsmtlib"
git = "https://github.com/onderjan/blazing-smtlib"
rev = "v0.1.0"
subDir = "lean"
```

## Usage

To read an SMT-LIB script from file and check its syntax is valid:

```lean
import BSmtLib
open BSmtLib

def main : IO Unit := do
  match ← readSmtFile SyntaxChecker.mk "file.smt2" with
  | .ok _ => IO.println "valid"
  | .error _ => throw (IO.userError "should be a valid SMT-LIB script")
```

`SyntaxChecker` is an instance of `Handler`, which handles SMT-LIB syntactic
categories (sorts, terms, etc.), and of `CommandHandler`, which handles standard
SMT-LIB commands (`assert`, `check-sat`, etc.). SMT solvers can implement
`Handler` and `CommandHandler` to handle SMT-LIB scripts, including custom
parsing of non-standard commands. See the handler documentation for details.

The function `readSmtFile` is a simplified caller for `Reader`, which supports
reading SMT-LIB scripts as well as other categories and commands. The reader is
itself a wrapper for the lexer (`BSmtLib.Lexer`) and a selectable parser that is
an instance of `Parser.Parse`; these can be used separately in applications
such as token pretty-printing or parsing user-constructed tokens.

Tokens and syntactic nodes can be written using `Writer`, which is configurable
for various whitespace formats including 1:1 reconstruction.

See the documentation of the modules and types of **BSmtLib** for additional
information. As the Lean documentation is ported from the Rust one without its
examples, see also the
[documentation of blazing-smtlib](https://docs.rs/blazing-smtlib); its
interface corresponds to the **BSmtLib** interface except for language-dependent
details.

## Usability

**BSmtLib** implements the whole syntax of SMT-LIB scripts for versions 2.0,
2.5, 2.6, and 2.7, including switching between versions. Tests of
**blazing-smtlib** and the 2025 SMT-LIB benchmark corpus are used to check that
**BSmtLib** behaves identically to it. Additional SMT-LIB grammar constructs
such as nonstandard commands or solver responses can be read using custom
parsers.

**BSmtLib** does not use any unsafe code and does not overflow the stack on
deeply nested input as its parsers nest into SMT-LIB constructs on the heap.
Unlike **blazing-smtlib**, which may panic on a broken contract, **BSmtLib** is
panic-free, as it requires its contracts to be met by proof objects.

## Specification and Proofs

The package also holds `BSmtLibSpec`, our formalised reading of the SMT-LIB
standard, versions 2.0, 2.5, 2.6, and 2.7, independent of **BSmtLib**. It is
checked against the standard's examples in
[`BSmtLibSpec/Facts.lean`](BSmtLibSpec/Facts.lean) and its submodules.

Various theorems about **BSmtLib** are stated and proven in
[`BSmtLib/Facts.lean`](BSmtLib/Facts.lean) and its submodules. For example:

- the script reader finishes exactly on the inputs that are SMT-LIB files, and
  tells the handler the calls for the file's commands;
- neither the lexer nor what the script reader tells a handler depends on how
  the input is cut into slices;
- what the script reader reads can be printed back exactly from SMT-LIB 2.5 on,
  and in 2.0 but for the spelling of string literals.

The proofs use only the three standard Lean axioms `propext`,
`Classical.choice`, and `Quot.sound`.

## Speed and Memory Consumption

**BSmtLib** is optimised for speed, with a custom-written fast-path lexer, token
batching, zero-copy tokens, and resource arenas. In a 14-job parallel run on AMD
Ryzen 9 9950X3D, given 9201 files sized from 200 kB to 200 MB from the 2025
SMT-LIB benchmark corpus
([non-incremental](https://zenodo.org/records/16740866) and
[incremental](https://zenodo.org/records/15493096)), each read from memory, it
lexed at 273 MB/s and parsed at 116 MB/s (geometric mean over files).

Parsers provided by **BSmtLib** call handlers in a streaming fashion. A parser
thus typically consumes a negligible amount of memory compared to a
user-provided handler that keeps lists or maps of symbols and terms or
constructs an abstract syntax tree.

## Lean Version

**BSmtLib** is built with Lean 4.34.1, pinned in `lean-toolchain`. As Lean does
not guarantee backwards compatibility, any release of **BSmtLib** may move to a
newer Lean.

## Licence

Licensed under either of Apache License, Version 2.0 or MIT licence at your
option. Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in this library by you, as defined in the Apache-2.0
licence, shall be dual licensed as above, without any additional terms or
conditions.

## Generative AI Policy

Generative AI (currently only Claude Code) is used for code review and help.
It makes no design decisions.

The main Rust library code is human-written, with the shape of its API
decided by the maintainer. Generative AI is used only for experiments and
for carefully reviewed changes where the semantic shape is clear but
implementing them by hand is cumbersome (when explicitly prompted to do them).
Generative AI is used to write benchmarks and tests (including unit tests).

The Lean library code is a port of the Rust library,
with generative AI used to port the code (as mechanically as possible).
The SMT-LIB corpus and library tests are used to check that both behave
identically.

Documentation (including examples) is either human-written or drafted
by generative AI and reviewed, then reworked if necessary. The Lean
documentation is ported from the Rust documentation by generative AI,
changed only where needed due to the language differences.

Commits are always done by a human, who reviews any generative-AI output.
Any commit that non-trivially uses generative AI either starts with
`Let <AI>...` (legacy style) or is signed off like so (new style):

```
Co-authored-by: <AI>
Signed-off-by: <committer>
```

See the [repository](https://github.com/onderjan/blazing-smtlib) for more
information.
