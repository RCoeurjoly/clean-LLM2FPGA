# SMT-LIB-Parser

https://smt-lib.org/papers/smt-lib-reference-v2.7-r2025-07-07.pdf

https://github.com/riaqn/smtlean/blob/master/src/smtlib.lean

https://www.philipzucker.com/lean_smt/

Alegere: parser recursiv descendent / bilioteca parser combinator (deja in biblioteca standard)


Pasul 1:

def parse : String -> Option Problem


Pasul 2:

def specification : Problem -> Prop
