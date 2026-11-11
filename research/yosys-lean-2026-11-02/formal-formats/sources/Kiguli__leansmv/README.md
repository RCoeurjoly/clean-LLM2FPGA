# VerifDemo — Lean 4 Verification Demo

A 30-minute demonstration of the Lean 4 theorem prover applied to:
1. **Discrete math** — set theory proofs (`VerifDemo/DiscreteMath.lean`)
2. **Transition systems** — reachable states and inductive invariants (`VerifDemo/TransitionSystem.lean`)
3. **NuXMV translation** — parse `.smv` models and verify invariants in Lean
   - Generator: `scripts/smv2lean.py`
   - Generated model: `VerifDemo/NuXMV/Counter.lean`
   - Manual proofs: `VerifDemo/NuXMV/CounterProofs.lean`
4. **Program verification** — IMP language, big-step semantics, Hoare logic (`VerifDemo/ProgramVerif/`)

## Build

```bash
lake build
```

## Regenerate Counter.lean from the SMV model

```bash
pip install -r scripts/requirements.txt       # first time only
python scripts/smv2lean.py models/counter.smv VerifDemo/NuXMV/Counter.lean
```

The translator handles:
- Enumerated types (`{off, on}` → `inductive ModeVal`)
- Boolean and bounded-nat variables
- `case...esac` → nested `if-then-else`
- `DEFINE` inlining (e.g., `count_max`)
- Nondeterministic inputs (variables without `init`/`next` get existentially quantified)
- Nondeterministic choice (`{a, b}` → disjunction)
- `next(x)` references across variables
- `INVARSPEC` → theorem statements (with `sorry` placeholders)

It has been tested on `counter.smv`, `traffic_light.smv`, and `mutex.smv`.

## Key files

| File | Purpose |
|------|---------|
| `VerifDemo/DiscreteMath.lean` | Subset transitivity, De Morgan, distributivity |
| `VerifDemo/TransitionSystem.lean` | `TransitionSystem`, `Reachable`, `InductiveInvariant`, soundness theorem |
| `VerifDemo/NuXMV/Counter.lean` | Generated Lean for the counter model |
| `VerifDemo/NuXMV/CounterProofs.lean` | Proves `x ≤ 10`, `mode=off → x=0`, `x>0 → mode=on` |
| `VerifDemo/NuXMV/Mutex.lean` | Generated Lean for the 2-process Peterson-style mutex |
| `VerifDemo/NuXMV/MutexProofs.lean` | Proves mutual exclusion for the 2-process mutex |
| `VerifDemo/Parametric/NMutex.lean` | **Parametric** N-process mutex, mutual exclusion proved for all N |
| `VerifDemo/ProgramVerif/Imp.lean` | IMP AST, big-step semantics, Hoare logic rules |
| `VerifDemo/ProgramVerif/Examples.lean` | Verified swap and counting-loop programs |
| `VerifDemo/ProgramVerif/Sort.lean` | **Insertion sort** — verified correct for all lists; testing vs verification |
| `slides/index.html` | Reveal.js slide deck for the 30-minute demo |
