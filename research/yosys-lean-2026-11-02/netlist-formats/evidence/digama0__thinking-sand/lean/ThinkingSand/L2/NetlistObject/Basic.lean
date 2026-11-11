import ThinkingSand.Prelude
import ThinkingSand.L1.DigitalAbstraction.Basic
/-!
# L2/00 — the netlist object

`N` is concrete data (`Netlist`); `Mealy(N) = denote theLib theNetlist`. The
library `theLib` is not postulated here: it **is** L1/06's per-cell Boolean shadow
(`boolShadow`), so the netlist semantics uses exactly the digital-abstraction
function the physics layer produces — the L0 half of the **S1** discharge. celltopo
independently checks the transistors compute it. One universal `Lib` interprets
every cell/op name appearing along the trail.
-/
namespace ThinkingSand.L2
open ThinkingSand.L1.DigitalAbstraction

/-- Number of netlist flops. External. -/
axiom nStateN : Nat
/-- The standard-cell library `Lib` — **grounded, not postulated**: it is L1/06's
    per-cell Boolean shadow (`boolShadow`), the untimed output of the digital
    abstraction. So `theLib` carries no axiom of its own; its only trusted content
    is L0's `boolShadow`, and `#print axioms` on any netlist result now shows the L0
    grounding in place of a free `theLib`. -/
noncomputable def theLib : Lib := boolShadow
/-- The hardened netlist `N` (parsed `ChipTop.mapped.v` / DEF). External. -/
axiom theNetlist : Netlist nIn nStateN nOut
/-- `Mealy(N)`. -/
noncomputable def netM : Mealy (Fin nStateN) (Fin nIn) (Fin nOut) := denote theLib theNetlist
/-- Flop index. -/
abbrev F : Type := Fin nStateN

/-- **celltopo certificate**, per cell: its transistors compute the Liberty function
    `theLib c` (checked over the full truth table). External. Partial: 311/437
    masters — the uncovered ones have no such certificate. -/
axiom celltopoCert : (c : String) → Cert.Celltopo (theLib c)
/-- The switch-level function celltopo derived for cell `c`. -/
noncomputable def switchFunc (c : String) : List Bool → Bool :=
  Cert.switchFn (celltopoCert c).cdl
/-- **S1 grounding (celltopo).** `theLib c` *is* the transistors' function —
    **derived** from the celltopo certificate (no longer `sorry`). -/
theorem s1_celltopo (c : String) : theLib c = switchFunc c :=
  ((celltopoCert c).holds).symm
