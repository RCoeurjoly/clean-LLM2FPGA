module

public import BSmtLib.Lexer
public import BSmtLib.Parser.Adapter
public import BSmtLib.Parser.Internal.Fused

public section

/-!
# Reading SMT-LIB input: lexing and parsing

Reading turns bytes into tokens (the `Lexer` module) and tokens into the syntactic categories
and commands of SMT-LIB (the `Parser` module), which are delivered to a `CommandHandler` in
streaming fashion: the input is processed piece by piece and every category or command to be
handled reaches the handler before the next one is read.

For simple usage, use `readSmt` and `readSmtFile`.

`Reader` reads the same way from any sequence of buffers and keeps the parser's resources
under the caller's control, so it is the entry point when the input does not arrive as one
buffered reader or when the resources should serve several reads. The `Lexer` and `Parser`
modules can also be used on their own.
-/

open BSmtLib.Lexer.Error BSmtLib.Parser.Adapter BSmtLib.Parser.Script BSmtLib.Handler

namespace BSmtLib.Reader

open Parser

/-- Why reading failed (I/O, lexical, syntax, version, or handler error). -/
inductive ReadErrorKind (E : Type) where
  /-- Opening or reading a file, or reading from a buffered reader, failed. -/
  | io (error : IO.Error)
  /--
  The input is not a valid sequence of SMT-LIB tokens, or a token follows what the parser
  accepts.
  -/
  | lexical (kind : LexicalErrorKind)
  /--
  The tokens do not form valid SMT-LIB syntactic categories or commands.

  A command whose name is not a standard command name of the version in force goes to
  `Handler.CommandHandler.nonstandardCommand`, so its rejection is a `ReadErrorKind.handler`
  error, e.g. `define-const` under SMT-LIB 2.6.
  -/
  | syntax (kind : SyntaxErrorKind)
  /-- The script declares a version that cannot be used. -/
  | version (kind : VersionErrorKind)
  /-- The handler rejected a command or a syntactic category. -/
  | handler (error : E)

/- LEAN: Rust's derived `Debug`. Lean's `IO.Error` has no `Repr`, so an I/O error is shown by its
message. -/
@[no_expose] instance {E : Type} [Repr E] : Repr (ReadErrorKind E) where
  reprPrec kind prec :=
    let app (name : String) (argument : Std.Format) : Std.Format :=
      Repr.addAppParen (.group (.nest 2 (.text ("BSmtLib.Reader.ReadErrorKind." ++ name) ++
        .line ++ argument))) prec
    match kind with
    | .io error => app "io" (reprArg (toString error))
    | .lexical kind => app "lexical" (reprArg kind)
    | .syntax kind => app "syntax" (reprArg kind)
    | .version kind => app "version" (reprArg kind)
    | .handler error => app "handler" (reprArg error)

/- LEAN: the constructor is private, as Rust's fields are, and the reader builds an error with
`ReadError.Internal.new`. The fields are readable as `error.kind` and `error.span`, replacing
the getters `kind`, `span` and `into_kind`. -/
/--
Error that forced reading to stop.

Its `ToString` gives the line and the column counted from one, while `Span.Location` counts
them from zero.
-/
structure ReadError (E : Type) where
  private mk ::
  /-- Returns the kind of the error. -/
  kind : ReadErrorKind E
  /--
  Returns the span where the error occurred.

  For I/O errors, the returned span is empty,
  indicating the position where the reading failed.
  -/
  span : Span
  deriving Repr

@[inline]
def ReadError.Internal.new {E : Type} (kind : ReadErrorKind E) (span : Span) : ReadError E :=
  { kind, span }

@[simp]
theorem ReadError.Internal.kind_new {E : Type} (kind : ReadErrorKind E) (span : Span) :
    (ReadError.Internal.new kind span).kind = kind :=
  (rfl)

@[simp]
theorem ReadError.Internal.span_new {E : Type} (kind : ReadErrorKind E) (span : Span) :
    (ReadError.Internal.new kind span).span = span :=
  (rfl)

/-- A read error is the one built from its fields. -/
@[simp]
theorem ReadError.Internal.new_eta {E : Type} (error : ReadError E) :
    ReadError.Internal.new error.kind error.span = error :=
  (rfl)

/-- Two read errors built from the same fields are equal. -/
theorem ReadError.Internal.new_inj {E : Type} {kind kind' : ReadErrorKind E}
    {span span' : Span} :
    ReadError.Internal.new kind span = ReadError.Internal.new kind' span' ↔
      kind = kind' ∧ span = span' := by
  constructor
  · intro h
    exact ⟨congrArg ReadError.kind h, congrArg ReadError.span h⟩
  · rintro ⟨rfl, rfl⟩
    rfl

instance {E : Type} : Inhabited (ReadError E) :=
  ⟨ReadError.Internal.new (.io (IO.userError "reading failed")) Span.Internal.newZero⟩

/--
Returns the most useful location for deciphering the error.

Guaranteed to be within the span.
-/
def ReadError.location {E : Type} (self : ReadError E) : Span.Location :=
  match self.kind with
  | .lexical kind => LexicalErrorKind.Internal.locationIn kind self.span
  | _ => self.span.start

/- LEAN: Rust's `Display` implementations are `ToString` instances, with the same text. -/
@[no_expose] instance {E : Type} [ToString E] : ToString (ReadErrorKind E) where
  toString
    | .io error => s!"I/O: {error}"
    | .lexical kind => toString kind
    | .syntax kind => toString kind
    | .version kind => toString kind
    | .handler error => toString error

@[no_expose] instance {E : Type} [ToString E] : ToString (ReadError E) where
  toString self :=
    let location := self.location
    s!"{self.kind} at line {location.line + 1}, column {location.column + 1}"

/- LEAN: Rust's private `from_parse`. It is public here as the statements about the reader's
errors in the facts about the public interface name it. -/
/-- Converts the error of a parser, located at `span`, into a read error. -/
def ReadError.ofParse {E : Type} (err : ParseError E) (span : Span) : ReadError E :=
  let kind := match err.kind with
    | .syntax err => ReadErrorKind.«syntax» err
    | .version err => ReadErrorKind.version err
    | .handler err => ReadErrorKind.handler err
  { kind, span }

/- LEAN: Rust's `impl From<SmtLexerError<SmtParseError<E>>> for SmtReadError<E>`, which Lean's
conversion by name stands for. -/
/-- Converts the error of a lexer over a parser into a read error. -/
def ReadError.ofLex {E : Type} (err : LexerError (ParseError E)) : ReadError E :=
  let span := err.span
  match err.kind with
  | .lexical kind => { kind := .lexical kind, span }
  | .consumer err => ReadError.ofParse err span

/- LEAN: not in Rust, whose reader takes `&mut self` and survives the error, and borrows the
handler, which the caller keeps. Here the failure carries the handler, as the failed call left it.
The failure will carry the reader when recovery is added. -/
/--
Failure of a read by a `Reader H P`, carrying the error that ended reading and the handler.
-/
structure ReadFailure (H P : Type) {m : Type → Type} [Monad m] {E R : Type} [Parse P H m E R]
    where
  /-- Error that ended reading. -/
  error : ReadError E
  /-- Handler, as reading left it at the error. -/
  handler : H

instance {H P : Type} {m : Type → Type} [Monad m] {E R : Type} [Parse P H m E R] [Inhabited H] :
    Inhabited (ReadFailure H P) :=
  ⟨{ error := default, handler := default }⟩

/- LEAN: not in Rust. The type of the reader's parser names the handler, so that the default
parser, made where it is used, can find the handler's script parser. -/
/-- A parser type for a reader over the handler `H`: any type. -/
abbrev Internal.ParserOf (_H : Type) : Type 1 := Type

/- LEAN: not in Rust: the default parser of `Reader`, which is Rust's `SmtScriptParser<H>`. -/
/-- The script parser over the handler, as a parser type for a reader over it. -/
abbrev Internal.scriptParser (H : Type) {m : Type → Type} [CommandHandler H m] :
    Internal.ParserOf H :=
  ScriptParser H

/- LEAN: Rust's reader holds a lexer over `SmtParseAdapter<'a, H, P>`, which borrows the
handler. Here the reader owns the handler, which `handler` and `modifyHandler` reach, and which
a failure carries.

Rust's bound `H: SmtHandler` is the instance argument `Handler H m`. Rust's default
parser `SmtScriptParser<H>` is the default of `P`, made where `Reader H` is written without a
parser (`Internal.scriptParser`), as Rust checks its default against the bounds only where it is
used: the script parser needs `CommandHandler H m`, which the bound `Handler H m` does not
give. -/
/--
Reads SMT-LIB input, delivering it to a handler.

A reader owns its lexer, its parser, the parser resources, and the handler. Any parser can be
used, and a convenience method `Reader.script` is provided for the common case of reading
SMT-LIB scripts.

The input is read either whole, through `readFile` or `readAll`, or in pieces, using
`readSlice` sequentially, followed by `readEof` once the input has ended. A piece may end
anywhere, even inside a token.

After the end of an input has been read without an error, the reader is reset and can be
reused. A call that returns an error gives no reader back, only the handler, which the failure
carries.

For whole inputs, consider `readSmtFile` and `readSmt`.
-/
structure _root_.BSmtLib.Reader (H : Type) {m : Type → Type} [Monad m] [Handler H m]
    (P : Internal.ParserOf H := by exact BSmtLib.Reader.Internal.scriptParser _) {E R : Type}
    [Parse P H m E R] where
  private mk ::
  private lexer : Lexer (ParseAdapter H P)

section

variable {H : Type} {m : Type → Type} [Monad m] [CommandHandler H m]

/--
Creates a reader of an SMT-LIB script, whose commands are delivered to `handler`.

The posture limits the versions that the script may declare; `initialVersion` is in force until
it declares one.
-/
def script (handler : H) (posture : Version.VersionPosture) (initialVersion : Version) :
    Reader H (ScriptParser H) :=
  { lexer := Lexer.new (ParseAdapter.new handler (ScriptParser.new posture)) initialVersion }

end

section

variable {H P : Type} {m : Type → Type} [Monad m] [Handler H m] {E R : Type} [Parse P H m E R]

/--
Creates a reader of the input `parser` accepts, over `handler`.

The input is read in `version`. Of the library parsers, only `ScriptParser` changes it, on a
version declaration.

The input must be exactly what the parser accepts: a token after it is a lexical error of the
kind `LexicalErrorKind.unexpectedTrailingToken`, and input that ends before it is a syntax
error.
-/
def new (handler : H) (parser : P) (version : Version) : Reader H P :=
  { lexer := Lexer.new (ParseAdapter.new handler parser) version }

/--
Returns the version currently in force.

The version can change if the token consumer asks for it from the cursor. The end of the input
resets the version to the initial one.
-/
def version (self : Reader H P) : Version :=
  self.lexer.version

/--
Returns the location from which the input is discarded, if any.

`ScriptParser` discards the input after the `(exit)` command; no other library parser discards.
The end of the input resets it to `none`, so after a successful `readAll`, `readFrom` or
`readFile` it is always `none`.
-/
def discardedFrom (self : Reader H P) : Option Span.Location :=
  self.lexer.discardedFrom

/-- Returns the handler. -/
def handler (self : Reader H P) : H :=
  self.lexer.consumer.handler

/- LEAN: Rust's `handler_mut`. The lexer and the adapter are taken apart so that the handler is
held once and changed in place, if the caller does not keep the old reader. -/
/-- Returns the reader with its handler changed by `f`. -/
def modifyHandler (self : Reader H P) (f : H → H) : Reader H P :=
  { lexer := self.lexer.modifyConsumer fun adapter => adapter.modifyHandler f }

namespace Internal

/- LEAN: not in Rust: the reader's lexer, which the proofs read, also where a call failed and
the reader is not given back. -/
/-- The lexer of the reader. -/
def lexer (self : Reader H P) : Lexer (ParseAdapter H P) :=
  self.lexer

/-- The reader with the lexer. -/
def ofLexer (lexer : Lexer (ParseAdapter H P)) : Reader H P :=
  { lexer }

end Internal

/--
Processes a byte slice as one piece of the input.

The piece may end anywhere, even inside a token: whatever is unfinished carries over to the next
call.

If an error occurs, the reader is not given back; the failure carries the handler.
-/
def readSlice (self : Reader H P) (slice : ByteSlice) :
    m (Except (ReadFailure H P) (Reader H P)) := do
  match ← self.lexer.lexSlice slice with
  | .ok lexer => return .ok ⟨lexer⟩
  | .error (err, adapter) =>
    return .error { error := ReadError.ofLex err, handler := adapter.handler }

/- LEAN: Rust's `read_eof` panics where the parser continues on the end of the input, a broken
contract; here it is an obligation on the caller, answered by `none`, as `Lexer.lexEof` does. -/
/--
Processes the end of the input.

Returns the located parser result if successful, and the reader, reset. A parser that continues
on the end of the input breaks its contract; the result is then `none`.

Call this once after the last `readSlice`; `readFile` and `readAll` call it themselves.

If an error occurs, the reader is not given back; the failure carries the handler.
-/
def readEof (self : Reader H P) :
    m (Except (ReadFailure H P) (Option (Span.Located R) × Reader H P)) := do
  match ← self.lexer.lexEof with
  | .ok (result, lexer) => return .ok (result, ⟨lexer⟩)
  | .error (err, adapter) =>
    return .error { error := ReadError.ofLex err, handler := adapter.handler }

/--
Reads the slice as the whole input.

Returns the located parser result if successful, `none` if the parser broke its contract (see
`readEof`), and the reader, reset.

If an error occurs, the reader is not given back; the failure carries the handler.
-/
def readAll (self : Reader H P) (input : ByteSlice) :
    m (Except (ReadFailure H P) (Option (Span.Located R) × Reader H P)) := do
  match ← self.readSlice input with
  | .ok self => self.readEof
  | .error err => return .error err

/- LEAN: Rust's `read_from` takes `&mut self` and the buffered reader; this takes the reader and
the buffered reader and gives both back, the buffered reader also after an error. The loop is the
recursion, a tail call; it is `partial`, as a buffered reader may give bytes without end.

Parity defect, to be fixed in a later version. After an I/O error, Rust's reader stays
usable and `read_from` can be called again to go on with the input. This gives back only the
handler after an I/O error too, as after any other error, and so does `readFile`. -/
/--
Reads the content of the buffered reader.

Returns the located parser result if successful, `none` if the parser broke its contract (see
`readEof`), and the reader, reset; and, in any case, the buffered reader.

The input is considered to end when the buffered reader returns a zero-length buffer. A failure
while reading is reported as an I/O error with an empty span at the position reached.

Once the parser requests discarding the rest of the input, e.g. after `(exit)` in a script,
the rest is left unread in the buffered reader.

If an error occurs, the reader is not given back; the failure carries the handler.
-/
@[specialize]
partial def readFrom {B : Type} {n : Type → Type} [Monad n] [MonadLiftT m n] [BufRead B n]
    (self : Reader H P) (bufReader : B) :
    n (Except (ReadFailure H P) (Option (Span.Located R) × Reader H P) × B) := do
  if self.discardedFrom.isSome then
    -- the lexer does not care about any subsequent bytes, break
    return (← (self.readEof : m _), bufReader)

  match ← BufRead.fillBuf bufReader with
  | .error err =>
    -- I/O error
    -- return an empty span on the current lexer location
    let location := self.lexer.currentLocation
    let span := Span.Internal.newEmpty location
    let kind := ReadErrorKind.io err
    return (.error { error := { kind, span }, handler := self.handler }, bufReader)
  | .ok (buffer, bufReader) =>
    if buffer.size == 0 then
      -- end of file reached
      -- end of stream, process EOF
      -- this will reset the lexer and return the break value
      return (← (self.readEof : m _), bufReader)

    -- remember the current offset and process the buffer
    let startOffset := self.lexer.currentLocation.offset
    match ← (self.readSlice buffer : m _) with
    | .error err => return (.error err, bufReader)
    | .ok self =>
      -- we need to consume the bytes read
      -- this might not be all bytes when discarding is set
      -- use the current lexer location offset minus our start offset
      let len := self.lexer.currentLocation.offset - startOffset
      readFrom self (BufRead.consume (n := n) bufReader len)

/- LEAN: Rust's `BufReader::with_capacity`, 1 MiB. -/
/-- The capacity of the buffer through which `readFile` reads a file. -/
def Internal.BUF_READER_CAPACITY : Nat := 1024 * 1024

/- LEAN: Rust's `path` is anything that is `AsRef<Path>`; here a `System.FilePath`, to which a
string is coerced. -/
/--
Opens the file at `path` and reads it.

Returns the located parser result if successful, `none` if the parser broke its contract (see
`readEof`), and the reader, reset.

A failure to open the file is reported as an I/O error with an empty span at the start of the
input. A failure while reading is reported as an I/O error with an empty span at the position
reached.

If an error occurs, the reader is not given back; the failure carries the handler.
-/
def readFile {n : Type → Type} [Monad n] [MonadLiftT IO n] [MonadLiftT m n] (self : Reader H P)
    (path : System.FilePath) :
    n (Except (ReadFailure H P) (Option (Span.Located R) × Reader H P)) := do
  let opened : IO (Except IO.Error IO.FS.Handle) := (IO.FS.Handle.mk path .read).toBaseIO
  match ← opened with
  | .error err =>
    return .error { error := { kind := .io err, span := Span.Internal.newZero }, handler :=
        self.handler }
  | .ok file =>
    -- make the buffered reader use a 1 MiB buffer so that we can
    -- lex/parse more efficiently with larger batches
    let bufReader := BufReader.new (IO.FS.Stream.ofHandle file) Internal.BUF_READER_CAPACITY
    let (result, _) ← self.readFrom bufReader
    return result

end

section

variable {H : Type} {m : Type → Type} [Monad m] [CommandHandler H m]

/- LEAN: Rust's `read_smt_file` lends the handler; here it is given and given back, also after
an error, in the failure. -/
/--
Reads an SMT-LIB script from a file, delivering it to a handler.

The script is read in SMT-LIB 2.7 until it declares its version, with the default posture
(`Version.VersionPosture.default`).

The handler is given back, in the failure if reading fails.

This is a convenience function over `Reader`: the parser is dropped after reading, so its
resources cannot be preserved for future use. Use `Reader` directly to keep the resources across
reads or to feed the input in pieces.

A failure to open the file is reported as an I/O error with an empty span at the start of the
input.
-/
def readSmtFile {n : Type → Type} [Monad n] [MonadLiftT IO n] [MonadLiftT m n] (handler : H)
    (path : System.FilePath) : n (Except (ReadFailure H (ScriptParser H)) H) := do
  let reader := Reader.script handler Version.VersionPosture.default default
  match ← reader.readFile path with
  | .ok (_, reader) => return .ok reader.handler
  | .error err => return .error err

/- LEAN: Rust's `read_smt` lends the handler and takes the buffered reader, which may be lent too;
here both are given and given back, also after an error: the handler in the failure, the buffered
reader as `readFrom` gives it back. -/
/--
Reads an SMT-LIB script from a buffered reader, delivering it to a handler.

The script is read in SMT-LIB 2.7 until it declares its version, with the default posture
(`Version.VersionPosture.default`).

The handler and the buffered reader are given back, the handler in the failure if reading
fails.

The input is considered to end when the buffered reader returns a zero-length buffer.

After `(exit)`, the rest of the input is left unread in the buffered reader.

This is a convenience function over `Reader`: the parser is dropped after reading, so its
resources cannot be preserved for future use. Use `Reader` directly to keep the resources across
reads or to feed the input in pieces.
-/
def readSmt {B : Type} {n : Type → Type} [Monad n] [MonadLiftT m n] [BufRead B n] (handler : H)
    (bufReader : B) : n (Except (ReadFailure H (ScriptParser H)) H × B) := do
  let reader := Reader.script handler Version.VersionPosture.default default
  match ← reader.readFrom bufReader with
  | (.ok (_, reader), bufReader) => return (.ok reader.handler, bufReader)
  | (.error err, bufReader) => return (.error err, bufReader)

end

end BSmtLib.Reader
