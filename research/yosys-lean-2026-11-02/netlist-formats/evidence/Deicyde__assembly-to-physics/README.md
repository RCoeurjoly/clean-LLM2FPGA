# AsmPhys: assembly-to-physics in Lean

A Lean 4 and Mathlib formalization of a chip-verification specification that runs from transistor
device contracts, through a nodal-ODE circuit model, logic cells, flip-flops and synchronous
refinement, to a probabilistic shell bounding the failure probability of a run. The specification
is the owner's (doc rev 46, in [docs/HANDOFF-2026-10-24.md](docs/HANDOFF-2026-10-24.md)).
[docs/STATUS.md](docs/STATUS.md) says what is done; the instruction-set and core layers (M5) and
the L0 bridge (M7) are not started.

## What is assumed and what is proved

The library has four layers. Only the first three need to be read to know what has been proved.

| Layer | Where | Contents | Status |
| --- | --- | --- | --- |
| Spec | [`AsmPhys/Spec/`](AsmPhys/Spec) | The trusted model: device contracts, the circuit model, the parameter sheet, logic levels, inverters and cells, clocking and the flip-flop contract, synchronous netlists | Trusted. Definitions only |
| Design | [`AsmPhys/Design/`](AsmPhys/Design) | The objects the claims are about: the flip-flop TGMS26 (netlist, hold tube, timing conditions) and the toy devices, sheet, inverter and flip-flop | Trusted. Definitions only |
| Claims | [`AsmPhys/Claims.lean`](AsmPhys/Claims.lean) | Every result, stated with Spec and Design definitions only, each hypothesis tagged | Proved |
| Proofs | everything else | Rail and band maximum principles, tubes, Lyapunov and charge functions, local inflows, inverter cores, transmission gates, latches, timing recursions | Checked by Lean; need not be read |

Spec files import only Mathlib and other Spec files; Design files import only Mathlib, Spec and
Design. Neither contains a theorem. A claim mentions only Spec, Design and Mathlib definitions, so a
mistake in a proof-layer definition can make a proof fail but cannot change what a claim says.

A Spec or Design definition that does not say what the physics or the specification intends makes
a claim mean something other than intended, and no proof can detect that. Those definitions are
the trusted model: about 1,200 lines with their documentation, each clause tagged with what
justifies it.

### What is assumed

1. **The model.** The definitions in `AsmPhys/Spec` fix the modelling choices: three-terminal
   transistors with quasi-static channel currents and no gate current, grounded nonlinear node
   capacitance, Kirchhoff's current law at every state node, trajectories as right-derivative
   solutions, logic values read through voltage thresholds, flip-flops specified by their contract,
   and synchronous netlists read as Mealy machines.
2. **The hypotheses of each claim.** Every hypothesis is tagged: LAW (a physical law), MODEL (a law
   plus a physical condition), MEAS (a measured or characterized bound), ENV (the operating
   environment), THM (discharged by a proof or a finite check elsewhere) or TCB (a trusted tool).
   A bundle such as `FETContract` carries its fields' tags. Premises that only constrain the
   claim's own variables (trajectories, initial states, observation times) carry none. No physical
   assumption is a global axiom.
3. **The foundations.** The Lean 4 kernel (v4.35.0), Mathlib (tag v4.35.0) and the axioms
   `propext`, `Classical.choice` and `Quot.sound`.

[docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) lists the modelling choices with their definitions, and
the tagged hypotheses with the claims that use them. Where a claim deviates from the specification
(a missing hypothesis, a strengthened condition), [docs/CHANGELOG.md](docs/CHANGELOG.md) records it
and [docs/FINDINGS.md](docs/FINDINGS.md) explains why.

### What is proved

[`AsmPhys/Claims.lean`](AsmPhys/Claims.lean) restates the library's results, each closed by the
theorem that proves it, so Lean checks that the restatement is what was proved.

| Claim | Spec theorem |
| --- | --- |
| `tellegen`, `tellegen_matrix` | THM L2.1 (Tellegen) |
| `exists_isTrajOn`, `isTrajOn_unique` | THM L2.2 (well-posedness) |
| `rail_invariance`, and the local forms `rail_invariance_set`, `rail_invariance_node` | THM L2.3 (rail invariance) |
| `exists_lipschitz_dependence`, `exists_dist_le_gronwallBound` | THM L2.4 (continuous dependence) |
| `slew_bound`, `slew_lipschitz`, and the pointwise form `abs_vf_le` | THM L2.5 (slew bound) |
| `inverter_propagation`, `inverter_contamination` | THM L3.1, THM L3.2 for INV |
| `inverter_behaves` | INV meets the cell abstraction used by THM L3.4 and THM L4.1 |
| `keeper_hold` | A cross-coupled inverter pair holds its bit |
| `clockOK_toSpec` | The adopted ClockOK implies the spec's |
| `tgff_contract` | TGMS26 meets the flip-flop contract FF1–FF2 (no leakage) |
| `buridan` | THM L3.3 (Buridan) |
| `combinational_timing` | THM L3.4 (combinational timing) |
| `refinement`, `refinement_out` | THM L4.1 (synchronous refinement) |
| `end_to_end` | THM End-to-end (probabilistic shell) |
| `exists_fetContract`, `sheet_consistent`, `toy_contract`, `exists_clockOK`, `toy_tgff_contract`, `toy_tgffHold_nonempty`, `toy_tgff_reachable` | Non-vacuity: the device contracts, the consistency conditions, ClockOK and the flip-flop contract are satisfiable |
| `inv_propagation_hypotheses`, `inv_rises`, `inv_falls`, `toy_buridan_hyps`, `toy_buridan` | Non-vacuity: every hypothesis of THM L3.1 (INV) and of THM L3.3 holds for a concrete circuit |

THM L3.4, THM L4.1 and `keeper_hold` have no non-vacuity witness yet (FINDINGS F13 for THM L4.1).

## Checks

```bash
lake exe cache get
```

```bash
lake build
```

```bash
python3 scripts/check_layers.py
```

```bash
lake env lean scripts/Audit.lean
```

- `lake build` checks every proof; the library contains no `sorry`.
- [`scripts/check_layers.py`](scripts/check_layers.py) checks that Spec and Design contain
  definitions only and import inward only; that `Claims.lean` contains only theorems; that no
  file defines notation, macros, syntax, elaborators, instances or attributes other than `simp`,
  so nothing outside Spec and Design can change how a claim's text elaborates; that no file uses
  `sorry`, `admit`, `axiom` or `native_decide`; and that every file is built.
- [`scripts/Audit.lean`](scripts/Audit.lean) checks, on the compiled library, that every library
  constant a claim's statement depends on, directly or through the definitions it uses, is
  defined in Spec or Design; that Spec and Design modules import only Mathlib and inward; and that
  every claim and every Spec and Design declaration uses only the three standard axioms. With
  `AUDIT_DEPS=1` it prints, for each claim, the Spec and Design definitions its statement depends
  on: the reading list for that claim.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs all three.

## Reading order

1. `Spec/Device.lean`: transistor contracts C1–C6 and node capacitance (L1).
2. `Spec/Circuit.lean`: nodes, transistors, netlists, the nodal ODE and trajectories (L2).
3. `Spec/Params.lean`: the parameter sheet and its consistency conditions K0–K14.
4. `Spec/Levels.lean`, `Spec/Cells.lean`, `Spec/FlipFlop.lean`: logic levels, inverters, cells,
   combinational DAGs, clocking and the flip-flop contract (L3).
5. `Spec/Sync.lean`: synchronous netlists, their Mealy semantics, U_ENV and Inv_L4 (L4).
6. `Design/Latch.lean`, `Design/TGFF.lean`, `Design/Toy.lean`.
7. `Claims.lean`.

## Documents

- [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md): every assumption, by kind and tag.
- [docs/STATUS.md](docs/STATUS.md): milestones, spec theorems and their proofs, witnesses.
- [docs/FINDINGS.md](docs/FINDINGS.md): problems found in the specification, for the owner.
- [docs/CHANGELOG.md](docs/CHANGELOG.md): every change of a statement, tag or hypothesis relative
  to the specification.
- [docs/SPIKE-FLIPFLOP.md](docs/SPIKE-FLIPFLOP.md): the transistor-level flip-flop study.
- [docs/HANDOFF-2026-10-24.md](docs/HANDOFF-2026-10-24.md): the specification and working rules.
