# Curated

Twenty hand-written proof obligations spanning mixed theories, plus one
import-only case that measures start-up cost so the others can be reported net
of it.

These are not drawn from a corpus. Each was written to isolate one capability,
which is the point: a corpus tells you how often a tool succeeds, and this tells
you what it succeeds *at*. Treat it as a regression suite that exposes concrete
capability differences, and validate any conclusion about overall strength on a
real corpus before relying on it.

## The cases

| Case | Capability |
| --- | --- |
| `00_import_only` | No goal; measures import and start-up time alone |
| `01_prop_chain` | Propositional implication chaining |
| `02_congruence` | Congruence closure over an uninterpreted function |
| `03_quantified` | Universally quantified premises |
| `04_existential` | Existential goals |
| `05_skolemization` | `∀ x, ∃ y` premise needing a function witness |
| `06_funext` | Functional extensionality |
| `07_higher_order` | Functions passed as arguments |
| `08_int_linear` | Linear integer arithmetic |
| `09_nat_sub` | Truncated natural subtraction |
| `10_option_injective` | Constructor injectivity on `Option` |
| `11_enum_pigeonhole` | Finite case analysis over an enumeration |
| `12_list_assoc` | Associativity of list append |
| `13_composition` | Function composition |
| `14_bool_mixed` | `Bool` and `Prop` in one goal |
| `15_bitvec` | `BitVec` commutativity |
| `16_string` | `String` reasoning |
| `17_empty` | Uninhabited type; SMT sorts are nonempty, `Empty` is not |
| `18_church_tower` | Higher-order Church numerals |
| `19_polymorphic` | Explicit polymorphic premises |
| `20_irrelevant_quantifier` | Quantified context the goal does not need |

Between them the cases mix propositional and quantified reasoning, equality and
congruence, linear integer and natural arithmetic, algebraic datatypes,
bitvectors, strings, and higher-order functions -- the theories an SMT-backed
tactic has to translate, and the ones it cannot.

## Running it

Every case closes its goal with `benchmark_tactic`. That one tactic is the
seam. `Curated/Harness.lean` binds it to `grind`, which needs nothing beyond
Lean itself, so the suite runs straight from a checkout:

```sh
lake build Curated
./run.sh 3          # repeats, default 3
```

`lake build` compiles the harness and no case, because only the root module is
a default target. A case elaborates when a driver names it.

## Branches

Each backend is its own branch. A branch changes exactly two files -- the
lakefile, to require that backend, and `Curated/Harness.lean`, to bind
`benchmark_tactic` to it. The cases are identical on every branch, so every
backend sees exactly the same obligation with exactly the same premises, and
none of them is mediated by a portfolio tactic.

| Branch | Backend |
| --- | --- |
| `main` | `grind`, Lean's own tactic; also the base the others branch from |
| `auto` | [lean-auto](https://github.com/leanprover-community/lean-auto), with Duper discharging its residual goal |
| `duper` | [Duper](https://github.com/leanprover-community/duper) directly |
| `lean-smt` | [lean-smt](https://github.com/ufmg-smite/lean-smt): cvc5 with Alethe replay in Lean |
| `crush` | [Crush](https://github.com/AD1024/lean-crush), configured through `crush.*` options |

Keeping them apart rather than behind one portfolio tactic is what makes the
comparison fair: no branch can borrow another's preprocessing, and a backend's
dependencies never enter another backend's environment.

For a driver that runs every branch and normalizes the results into one report,
see [lean-crush](https://github.com/AD1024/lean-crush) and its
`scripts/benchmark-curated.sh`.

## Provenance

The cases were written for lean-crush's evaluation and previously lived in a
fork of LeanHammer, which made them read as LeanHammer's own benchmark. They
were not, and the fork existed only to host them. The suite stands alone here
and depends on no part of LeanHammer.
