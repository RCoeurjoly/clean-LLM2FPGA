module

public import BSmtLib.Facts.Lexer
public import BSmtLib.Facts.Parser
public import BSmtLib.Facts.Reader
public import BSmtLib.Facts.PublicApi

@[expose] public section

/-!
# Facts about the library

What the library satisfies, one module for each part:

- with respect to the specification formalism (`BSmtLibSpec`):
  - the lexers (`Facts.Lexer`);
  - the parsers (`Facts.Parser`);
  - the readers (`Facts.Reader`);
- stated with the public API rather than the formalism: the mainline theorems below, and the
  others in `Facts.PublicApi`.

Each part states its theorems, each followed by the axioms its proof uses, and checks the
adapters that its statements are made with. The proofs are below `Facts.Proofs`.

## The mainline theorems

The main theorems about the public API are stated here; `Facts.PublicApi` states the others:

- the lexer's tokens cover the input without a gap or an overlap (`lexing_lossless`);
- the lexer does not depend on how the input is cut into slices (`lexing_slicing`), and neither
  does what the script reader tells a handler that sees contents only (`reading_slicing`);
- what the script reader tells such a handler can be printed back as the input, up to where the
  reader began to discard it: in 2.0 but for the spelling of string literals, and exactly from
  2.5 on (`reprinting`);
- the lexers of the chain hand over the same tokens as the library's lexer and fail with errors
  of the same cause (`lexers_agree`).

They are stated as those of `Facts.PublicApi` are, with the names a user writes, bytes, lists and
the small definitions below `BSmtLib.Facts.PublicApi.Adapters`, and checked in the same way at
the end. `lexers_agree` also names the lexers of the chain, which it compares.
-/

namespace BSmtLib.Facts

open BSmtLib.Facts.PublicApi.Adapters
open BSmtLib.Facts.PublicApi.Adapters.Reprinter BSmtLib.Facts.PublicApi.Adapters.Chain
open BSmtLib (Version)

/-! ## Reprinting -/

/--
What the script reader reads can be printed back with the same content (2.0) or exactly (2.5+).

There is a handler that is told contents only, with a printer of what it was told. When the
script reader over this handler reads an input to the end, the printer gives back the input up to
where the reader began to discard it. The input may be fed in any slices.

The input, up to where the reader began to discard it or else to its end, is the printed tokens
with layout around them (`Spaced`):

- each token is non-empty;
- every comment in the layout ends with a line break;
- only the layout after the last token may end with a comment that has no line break.

This holds in two cases:

- the reader allows only 2.0 and starts in it (`Reprints20`), and a string literal may come back
  as another literal that stands for the same bytes in 2.0;
- the reader allows only the versions from 2.5 on and starts in one of them
  (`ReprintsExactly25Plus`), and the input comes back exactly.

The cases differ because string literals are escaped differently:

- in 2.0, a backslash escapes `"` and `\` and stands for itself before any other byte, so `"\a"`
  and `"\\a"` stand for the same bytes, and since the handler is told only the bytes, the printer
  cannot tell which of the two was written;
- from 2.5 on, a backslash is an ordinary byte and `""` stands for `"`, so every literal has only
  one spelling.

A script that switches between 2.0 and a later version is covered by neither case: a statement
about it would have to say in which version each literal was read.

The handler fails on a command that is not standard (`ContentHandler`), so the reader does not
read an input with such a command to the end, and the theorem says nothing of it.
-/
theorem reprinting : ∃ r : Reprinter, r.Reprints20 ∧ r.ReprintsExactly25Plus :=
  ⟨Proofs.PublicApi.Reprinting.recorder, Proofs.PublicApi.Reprinting.recorder_Reprints20,
    Proofs.PublicApi.Reprinting.recorder_ReprintsExactly25Plus⟩

/--
info: 'BSmtLib.Facts.reprinting' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in
#print axioms reprinting

/-! ## Lossless lexing -/

/--
The library's lexer hands over tokens whose spans cover the input without a gap or an overlap:

- the whole input, ending with the end of the input, where no error occurs;
- the input from its start up to at most where the error is, at an error.

Its consumer takes every token, whitespace and comments included. The lexer is fed the input in
any slices and then the end of the input.
-/
theorem lexing_lossless (version : Version) (slices : List ByteList) :
    (∀ result entries, collect version slices = (.ok result, entries) →
      Tiles entries 0 slices.flatten.length) ∧
    (∀ error entries, collect version slices = (.error error, entries) →
      ∃ stop, Tiles entries 0 stop ∧ stop ≤ error.span.start.offset) := by
  have lossless := Proofs.PublicApi.Lossless.lossless version slices
  refine ⟨fun _ _ run => ?_, fun _ _ run => ?_⟩ <;> rw [run] at lossless <;> exact lossless

/--
info: 'BSmtLib.Facts.lexing_lossless' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in
#print axioms lexing_lossless

/-! ## Slicing invariance -/

/--
The library's lexer, with a consumer that takes every token, hands over the same tokens and ends
the same way however the input is cut into slices.
-/
theorem lexing_slicing (version : Version) (slices : List ByteList) :
    collect version slices = collect version [slices.flatten] :=
  Proofs.PublicApi.Slicing.slicing version slices

/--
info: 'BSmtLib.Facts.lexing_slicing' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in
#print axioms lexing_slicing

/--
The script reader does not tell how its input is cut into slices.

There is a handler told contents only, with a printer as in `reprinting` (`Reprints20`,
`ReprintsExactly25Plus`). The script reader over it, allowing every version and starting in any of
them, ends the same way however the input is cut: with the same error, or with the same result,
the same printed tokens and the same location where it began to discard.
-/
theorem reading_slicing :
    ∃ r : Reprinter, r.Reprints20 ∧ r.ReprintsExactly25Plus ∧
      ∀ version slices, r.observe (readThrough (r.reader version) slices) =
        r.observe (readThrough (r.reader version) [slices.flatten]) :=
  ⟨Proofs.PublicApi.Reprinting.recorder, Proofs.PublicApi.Reprinting.recorder_Reprints20,
    Proofs.PublicApi.Reprinting.recorder_ReprintsExactly25Plus,
    Proofs.PublicApi.ReaderSlicing.recorder_slicing⟩

/--
info: 'BSmtLib.Facts.reading_slicing' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in
#print axioms reading_slicing

/-! ## The lexers of the chain agree -/

/--
The naive lexer, the Rust port, the lexer with the batch kept in its state and the library's
lexer hand over the same tokens and end the same way. They fail with errors of the same cause;
where an error is located is not compared. Each has a consumer that takes every token and is fed
the same input in the same slices, whatever they are.
-/
theorem lexers_agree (version : Version) (slices : List ByteList) :
    causeOnly (collectV version slices) = causeOnly (collect version slices) ∧
      causeOnly (collectW version slices) = causeOnly (collect version slices) ∧
      causeOnly (collectX1 version slices) = causeOnly (collect version slices) :=
  Proofs.PublicApi.Agree.lexers_agree version slices

/--
info: 'BSmtLib.Facts.lexers_agree' depends on axioms: [propext, Classical.choice, Quot.sound]
-/
#guard_msgs in
#print axioms lexers_agree

/-! ## What the statements rest on -/

/--
info: 5 theorems, stated with 61 adapters:
BSmtLib.Facts.PublicApi.Adapters:
  BSmtLib.Facts.PublicApi.Adapters.ByteList
  BSmtLib.Facts.PublicApi.Adapters.CollectM
  BSmtLib.Facts.PublicApi.Adapters.Collector
  BSmtLib.Facts.PublicApi.Adapters.Entry
  BSmtLib.Facts.PublicApi.Adapters.FinalLayout
  BSmtLib.Facts.PublicApi.Adapters.Layout
  BSmtLib.Facts.PublicApi.Adapters.SameSpelling
  BSmtLib.Facts.PublicApi.Adapters.SameSpellings
  BSmtLib.Facts.PublicApi.Adapters.Spaced
  BSmtLib.Facts.PublicApi.Adapters.Tiles
  BSmtLib.Facts.PublicApi.Adapters.allVersions
  BSmtLib.Facts.PublicApi.Adapters.collect
  BSmtLib.Facts.PublicApi.Adapters.collectWith
  BSmtLib.Facts.PublicApi.Adapters.feed
  BSmtLib.Facts.PublicApi.Adapters.instConsumeCollectorUnitEmpty
  BSmtLib.Facts.PublicApi.Adapters.instConsumeFromCollectorLexerCursorCollectMUnitEmpty
  BSmtLib.Facts.PublicApi.Adapters.readThrough
  BSmtLib.Facts.PublicApi.Adapters.readUpTo
  BSmtLib.Facts.PublicApi.Adapters.resultOf
  BSmtLib.Facts.PublicApi.Adapters.unescape20
BSmtLib.Facts.PublicApi.Adapters.Chain:
  BSmtLib.Facts.PublicApi.Adapters.Chain.causeOnly
  BSmtLib.Facts.PublicApi.Adapters.Chain.collectBy
  BSmtLib.Facts.PublicApi.Adapters.Chain.collectV
  BSmtLib.Facts.PublicApi.Adapters.Chain.collectW
  BSmtLib.Facts.PublicApi.Adapters.Chain.collectX1
BSmtLib.Facts.PublicApi.Adapters.Content:
  BSmtLib.Facts.PublicApi.Adapters.Content.AttributeValueNode
  BSmtLib.Facts.PublicApi.Adapters.Content.Constant
  BSmtLib.Facts.PublicApi.Adapters.Content.Constructor
  BSmtLib.Facts.PublicApi.Adapters.Content.ContentHandler
  BSmtLib.Facts.PublicApi.Adapters.Content.ContentHandler.instCommandHandler
  BSmtLib.Facts.PublicApi.Adapters.Content.ContentHandler.instHandler
  BSmtLib.Facts.PublicApi.Adapters.Content.Identifier
  BSmtLib.Facts.PublicApi.Adapters.Content.Index
  BSmtLib.Facts.PublicApi.Adapters.Content.Pattern
  BSmtLib.Facts.PublicApi.Adapters.Content.SExprNode
  BSmtLib.Facts.PublicApi.Adapters.Content.Signature
  BSmtLib.Facts.PublicApi.Adapters.Content.Symbol
  BSmtLib.Facts.PublicApi.Adapters.Content.SymbolOrWildcard
  BSmtLib.Facts.PublicApi.Adapters.Content.TermNode
  BSmtLib.Facts.PublicApi.Adapters.Content.contentOf
  BSmtLib.Facts.PublicApi.Adapters.Content.ofAttributeValueNode
  BSmtLib.Facts.PublicApi.Adapters.Content.ofConstant
  BSmtLib.Facts.PublicApi.Adapters.Content.ofConstructor
  BSmtLib.Facts.PublicApi.Adapters.Content.ofIdentifier
  BSmtLib.Facts.PublicApi.Adapters.Content.ofIndex
  BSmtLib.Facts.PublicApi.Adapters.Content.ofPairs
  BSmtLib.Facts.PublicApi.Adapters.Content.ofPattern
  BSmtLib.Facts.PublicApi.Adapters.Content.ofSExprNode
  BSmtLib.Facts.PublicApi.Adapters.Content.ofSignature
  BSmtLib.Facts.PublicApi.Adapters.Content.ofSymbol
  BSmtLib.Facts.PublicApi.Adapters.Content.ofSymbolOrWildcard
  BSmtLib.Facts.PublicApi.Adapters.Content.ofTermNode
BSmtLib.Facts.PublicApi.Adapters.Reprinter:
  BSmtLib.Facts.PublicApi.Adapters.Reprinter
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.Reprints20
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.ReprintsExactly25Plus
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.from25
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.instContentHandlerH
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.observe
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.only20
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.reader
  BSmtLib.Facts.PublicApi.Adapters.Reprinter.readerWith
-/
#guard_msgs in
#adapters within BSmtLib.Facts.PublicApi

/--
info: BSmtLib.Facts.lexers_agree:
  small: Adapters.ByteList, Adapters.Chain.causeOnly, Adapters.Chain.collectBy,
    Adapters.Chain.collectV, Adapters.Chain.collectW, Adapters.Chain.collectX1, Adapters.CollectM,
    Adapters.Collector, Adapters.Entry, Adapters.collect, Adapters.collectWith,
    Adapters.instConsumeCollectorUnitEmpty,
    Adapters.instConsumeFromCollectorLexerCursorCollectMUnitEmpty, Adapters.resultOf
  chain: BSmtLib.Lexer.Internal.VNaive.Lexer, BSmtLib.Lexer.Internal.VNaive.Lexer.AfterEofError,
    BSmtLib.Lexer.Internal.VNaive.Lexer.AfterError, BSmtLib.Lexer.Internal.VNaive.Lexer.lexEof,
    BSmtLib.Lexer.Internal.VNaive.Lexer.lexSlice, BSmtLib.Lexer.Internal.VNaive.Lexer.new,
    BSmtLib.Lexer.Internal.WRustPort.Lexer, BSmtLib.Lexer.Internal.WRustPort.Lexer.AfterEofError,
    BSmtLib.Lexer.Internal.WRustPort.Lexer.AfterError, BSmtLib.Lexer.Internal.WRustPort.Lexer.lexEof,
    BSmtLib.Lexer.Internal.WRustPort.Lexer.lexSlice, BSmtLib.Lexer.Internal.WRustPort.Lexer.new,
    BSmtLib.Lexer.Internal.X1BatchInCore.Lexer.lexEof,
    BSmtLib.Lexer.Internal.X1BatchInCore.Lexer.lexSlice
BSmtLib.Facts.lexing_lossless:
  small: Adapters.ByteList, Adapters.CollectM, Adapters.Collector, Adapters.Entry, Adapters.Tiles,
    Adapters.collect, Adapters.collectWith, Adapters.instConsumeCollectorUnitEmpty,
    Adapters.instConsumeFromCollectorLexerCursorCollectMUnitEmpty, Adapters.resultOf
BSmtLib.Facts.lexing_slicing:
  small: Adapters.ByteList, Adapters.CollectM, Adapters.Collector, Adapters.Entry, Adapters.collect,
    Adapters.collectWith, Adapters.instConsumeCollectorUnitEmpty,
    Adapters.instConsumeFromCollectorLexerCursorCollectMUnitEmpty, Adapters.resultOf
BSmtLib.Facts.reading_slicing:
  small: Adapters.ByteList, Adapters.Content.AttributeValueNode, Adapters.Content.Constant,
    Adapters.Content.Constructor, Adapters.Content.ContentHandler,
    Adapters.Content.ContentHandler.instCommandHandler, Adapters.Content.ContentHandler.instHandler,
    Adapters.Content.Identifier, Adapters.Content.Index, Adapters.Content.Pattern,
    Adapters.Content.SExprNode, Adapters.Content.Signature, Adapters.Content.Symbol,
    Adapters.Content.SymbolOrWildcard, Adapters.Content.TermNode, Adapters.Content.contentOf,
    Adapters.Content.ofAttributeValueNode, Adapters.Content.ofConstant,
    Adapters.Content.ofConstructor, Adapters.Content.ofIdentifier, Adapters.Content.ofIndex,
    Adapters.Content.ofPairs, Adapters.Content.ofPattern, Adapters.Content.ofSExprNode,
    Adapters.Content.ofSignature, Adapters.Content.ofSymbol, Adapters.Content.ofSymbolOrWildcard,
    Adapters.Content.ofTermNode, Adapters.FinalLayout, Adapters.Layout, Adapters.Reprinter,
    Adapters.Reprinter.Reprints20, Adapters.Reprinter.ReprintsExactly25Plus,
    Adapters.Reprinter.from25, Adapters.Reprinter.instContentHandlerH, Adapters.Reprinter.observe,
    Adapters.Reprinter.only20, Adapters.Reprinter.reader, Adapters.Reprinter.readerWith,
    Adapters.SameSpelling, Adapters.SameSpellings, Adapters.Spaced, Adapters.allVersions,
    Adapters.feed, Adapters.readThrough, Adapters.readUpTo, Adapters.unescape20
BSmtLib.Facts.reprinting:
  small: Adapters.ByteList, Adapters.Content.AttributeValueNode, Adapters.Content.Constant,
    Adapters.Content.Constructor, Adapters.Content.ContentHandler,
    Adapters.Content.ContentHandler.instCommandHandler, Adapters.Content.ContentHandler.instHandler,
    Adapters.Content.Identifier, Adapters.Content.Index, Adapters.Content.Pattern,
    Adapters.Content.SExprNode, Adapters.Content.Signature, Adapters.Content.Symbol,
    Adapters.Content.SymbolOrWildcard, Adapters.Content.TermNode, Adapters.Content.contentOf,
    Adapters.Content.ofAttributeValueNode, Adapters.Content.ofConstant,
    Adapters.Content.ofConstructor, Adapters.Content.ofIdentifier, Adapters.Content.ofIndex,
    Adapters.Content.ofPairs, Adapters.Content.ofPattern, Adapters.Content.ofSExprNode,
    Adapters.Content.ofSignature, Adapters.Content.ofSymbol, Adapters.Content.ofSymbolOrWildcard,
    Adapters.Content.ofTermNode, Adapters.FinalLayout, Adapters.Layout, Adapters.Reprinter,
    Adapters.Reprinter.Reprints20, Adapters.Reprinter.ReprintsExactly25Plus,
    Adapters.Reprinter.from25, Adapters.Reprinter.instContentHandlerH, Adapters.Reprinter.only20,
    Adapters.Reprinter.readerWith, Adapters.SameSpelling, Adapters.SameSpellings, Adapters.Spaced,
    Adapters.feed, Adapters.readThrough, Adapters.readUpTo, Adapters.unescape20
-/
#guard_msgs in
#trusts within BSmtLib.Facts.PublicApi

end BSmtLib.Facts
