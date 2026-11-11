# blazing-smtlib & BSmtLib

Fast, safe, dependency-free SMT-LIB parsing.

The Rust library **blazing-smtlib** is a high-performance strict parsing library
for [SMT-LIB 2](https://smt-lib.org/) with no dependencies and no unsafe code.
**BSmtLib** is a port of **blazing-smtlib** to Lean 4, with the same
architecture and public interface, adapted to Lean where needed.

## Repository structure

- [`rust/`](rust/README.md) – **blazing-smtlib**, the Rust library.
  - [See the README.](rust/README.md)
  - Published on [crates.io](https://crates.io/crates/blazing-smtlib).
  - Documentation available on [docs.rs](https://docs.rs/blazing-smtlib).
- [`lean/`](lean/README.md) – **BSmtLib**, the Lean port of the library.
  - [See the README.](lean/README.md)
- [`tools/`](tools/) – Tools for running Rust tests against **BSmtLib**.

## Usability

Both **blazing-smtlib** and **BSmtLib** are zero-dependency libraries that do
not use any unsafe code and are not susceptible to stack overflow by design.
Additionally, **BSmtLib** is panic-free.

The libraries implement the whole syntax of SMT-LIB scripts for versions 2.0,
2.5, 2.6, and 2.7, including switching between versions, and can parse all files
from the 2025 SMT-LIB benchmark corpus. Additional SMT-LIB grammar constructs
such as nonstandard commands or solver responses can be read using custom
parsers.

## Speed and memory usage

The libraries are optimised for speed, with a custom-written fast-path lexer,
token batching, zero-copy tokens, and resource arenas.

| Library                   | Lexing (MB/s) | Parsing (MB/s) |
| ------------------------- | ------------: | -------------: |
| **blazing-smtlib** (Rust) |          1134 |            497 |
| **BSmtLib** (Lean)        |           273 |            116 |

The figures are geometric means over 9201 files from the 2025 SMT-LIB benchmark
corpus ([non-incremental](https://zenodo.org/records/16740866) and
[incremental](https://zenodo.org/records/15493096)) from 200 kB to 200 MB drawn
by SMT-COMP-style rules, each read from memory, in a 14-job parallel run on an
AMD Ryzen 9 9950X3D.

Parsers provided by the libraries call handlers in a streaming fashion. A parser
thus typically consumes a negligible amount of memory compared to a
user-provided handler that keeps lists or maps of symbols and terms or
constructs an abstract syntax tree.

## Licence

Licensed under either of Apache License, Version 2.0 or MIT licence at your
option. Unless you explicitly state otherwise, any contribution intentionally
submitted for inclusion in this repository by you, as defined in the Apache-2.0
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
