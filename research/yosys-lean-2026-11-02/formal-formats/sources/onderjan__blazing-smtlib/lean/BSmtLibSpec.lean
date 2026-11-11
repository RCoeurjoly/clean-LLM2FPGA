module

public import BSmtLibSpec.Regex
public import BSmtLibSpec.Lexicon
public import BSmtLibSpec.Lexicon.Tokenization
public import BSmtLibSpec.Lexicon.Properties
public import BSmtLibSpec.Grammar
public import BSmtLibSpec.Grammar.Tree
public import BSmtLibSpec.Grammar.Syntax
public import BSmtLibSpec.Grammar.File
public import BSmtLibSpec.Grammar.Script
public import BSmtLibSpec.Grammar.Properties
public import BSmtLibSpec.Lexer
public import BSmtLibSpec.Parser
public import BSmtLibSpec.Parser.VersionPolicy
public import BSmtLibSpec.Parser.Traversal
public import BSmtLibSpec.Parser.Properties
public import BSmtLibSpec.Reader

@[expose] public section

/-!
# Our reading of the SMT-LIB 2 standard

Our formalised reading of the SMT-LIB standard, versions 2.0, 2.5, 2.6 and 2.7,
against which a lexer and a parser can be proved correct.
Its sources of truth are the specification files of the respective versions.

- `Regex` is the formalism the lexicon is written in.
- `Lexicon` formalises the lexicon (lexical categories, tokens, and tokenisation).
- `Grammar` formalises the grammar for syntactic categories and commands.
  It does not include the syntax of theories, logics, and command responses,
  and considers restrictions for info flags and command options semantic.
- `Lexer` formalises the expected observable behaviour of a lexer under our reading,
  fed in slices, and how it answers a consumer that breaks, switches the version or has
  the rest of the input discarded.
- `Parser` formalises the expected observable behaviour of a parser under our reading.
- `Reader` formalises what a reader, a lexer with a parser as its consumer, tells its handler.

`Facts` prove properties of the formalism, including its correspondence to the examples
in the standard. We note that the proofs depend on the formal statement of the property
being proven as well as the formalism: if the property does not accurately formalise
what its comment says, proving it does not give credibility to what the comment promises.

## Version releases

The following standard version releases are followed:
 - Version 2.7: release 2026-09-09.
 - Version 2.6: release 2024-09-20.
 - Version 2.5: release 2015-06-28.
 - Version 2.0: release 2012-09-09.

Notably, reserved words are allowed in S-expressions from 2.6 release 2021-04-02 onwards.
Release 2017-07-18, the first release of 2.6, did not allow them.

## References to the standard

The documentation comments of every definition that is based on the standard
refer to the version of the standard and the relevant section of the specification.
Behaviour that did not change between versions is only referenced to the latest version.

We use the following comments at every definition that states a rule or a property based
on the standard, except where documented otherwise in the file-level documentation comment.
The modules `Lexer`, `Parser` and `Reader` formalise our reading of how a lexer, a parser and
a reader behave, of which the standard says little; their definitions carry no tag except
*Property*.

- **Standard:** the definition formalises a rule of the standard at the version and place given,
  and includes the version, section, and the name of production in Appendix B where relevant.
- **Reading:** our decision how to read the standard
  when it is silent, ambiguous, or seemingly contradictory.

Additionally, we use the following comments with a defined meaning:

- **Property:** a proposition that can be proved about the formalised reading
  or an implementation against the formalised reading. Properties are devised
  by us to formalise expectations about the implementation behaviour under SMT-LIB,
  and to form correctness theorems.
- **Formalism:** our decision on how to formalise the given item.

## List of readings

Lexicon:
- **L1**: consecutive non-parenthesis tokens must be separated (maximal-munch not allowed).
- **L2**: an unterminated comment at the end of the input is not an error.
- **L3**: a letter is `a` to `z` or `A` to `Z`.
- **L4**: a simple symbol excludes reserved words.
- **L5**: a keyword is `:` followed by simple-symbol characters,
         allowing reserved words and leading digits but not an empty continuation.
- **L6**: `define-funs-rec` is a command name (and a reserved word) from 2.5 on.
- **L7**: a 2.0 string literal may contain TAB, CR, or LF.
- **L8**: the limitation of 2.0 to US-ASCII limits the input, not the lexicon.

Grammar:
- **G1**: before 2.6, a reserved word is not an s-expression.

Syntactic modes:
- **M1**: a version declaration changes the syntax mode between commands after
  the closing parenthesis of the `set-info` command; a declaration of a version
  that is not supported (such as 3.0) cannot stand in a file; `:smt-lib-version`
  declares the version in all SMT-LIB 2 versions including 2.0 and 2.5 which do
  not state this explicitly.
- **M2**: `exit` ends a script; the input after it is not parsed.
- **M3**: `reset` does not return to the initial version.

## Details and reasoning

### Lexicon

**L1**: The standard does not specify how adjacent tokens are separated.
A state-machine lexer that continues a token until it is no longer possible
and then immediately lexes from the normal states (maximal-munch) makes `123abc`
a valid sequence of a numeral `123` followed by a symbol `abc`, the same as `123 abc`.
We believe this is not the intention, so we require separation by parentheses,
comments, or whitespace.

**L2**: In §3.1, it is specified that a comment ends with CR or LF, but does not specify
if this end must appear. As this would be a special error condition on comments,
which should fulfil the function of whitespace that has no error conditions,
we do not require the ending line break.

**L3**: In version 2.5 Appendix B, ⟨letter⟩ is written as `A | ··· | A | a | ··· | z`.
We believe this is a typo, and it is fixed in Appendix B of version 2.6 and 2.7.

**L4**: Only Appendix B of 2.7 specifies that a simple symbol is not a reserved word.
The previous versions did so in §3.1 but not in Appendix B. We believe this was a typo.

**L5**: From version 2.5, ⟨keyword⟩ is written in Appendix B `:`⟨simple_symbol⟩.
This would forbid a keyword starting with a digit or naming a reserved word.
The standard itself gives an example of `:56` as a keyword, and the restriction
on reserved words is not syntactically necessary. Therefore, we allow leading digits
and reserved words in keywords, but still forbid `:` as a complete token.

**L6**: From 2.5 on, `define-funs-rec` is a command, but is not included in the list
of reserved words in Appendix B. We believe this is an unintentional omission,
as §3.1 makes every command name a reserved word.

**L7**: Version 2.0 §3.1 defines a string literal as a sequence of printable ASCII characters.
Quoted symbols are defined using "printable ASCII characters (including space, tab, and
line-breaking characters)". As TAB, CR, and LF are not printable characters in ASCII,
we believe the string-literal printability includes TAB, CR, and LF in 2.0.
In later versions, these are explicitly allowed.

**L8**: Version 2.0 §3.1 restricts source files to ASCII. We read this not as a lexical rule
that should be checked by the lexer, but a rule restricting the input: the driver is
responsible for enforcing it if needed, not the lexer. The ASCII rule is syntactically
unnecessary and was removed in later versions.

### Grammar

**G1**: In 2.0, 2.5 and release 2017-07-18 of 2.6, §3.2 calls an S-expression "either a
non-parenthesis token or a (possibly empty) sequence of S-expressions enclosed in parentheses",
which includes reserved words, but its production ⟨s_expr⟩, in §3.2 and Appendix B, does not
include ⟨reserved⟩. Release 2021-04-02 of 2.6 is "now allowing reserved words in s-expression",
which says they were not allowed before. We follow the production: in 2.0 and 2.5, a reserved
word is not an s-expression.

### Syntactic modes

**M1**: We consider a version declaration using `set-info` command with `:smt-lib-version`
to change the syntactic mode (including lexing) exactly after the closing parenthesis
of the `set-info` command: the command ends first, then the version switch occurs,
then new commands can be read in under a new version declaration.
The standard defines the syntax of the versions it specifies, and none for any other,
so a declaration of a version that is not one of them (such as 3.0) names no mode in which
the rest could be read: it cannot stand in a file.
The standard defines the attribute `:smt-lib-version` from 2.6 on (2.6 and 2.7 §4.2.9);
2.0 and 2.5 do not mention it. We give it the same meaning in 2.0 and 2.5, so that a file
read in any version can declare the version it continues in; there too, a declaration whose
value is not the number of a supported version cannot stand.

**M2**: We consider `(exit)` to change the syntactic mode to a mode where all further
bytes are discarded, exactly after the closing parenthesis. The end of the input is not one
of those bytes: it still follows them, and ends the script as it ends any other.

**M3**: We consider `(reset)` to be a reset of the semantic state, not the syntactic mode,
so a previous version declaration using `set-info` is still honoured after reset.

-/
