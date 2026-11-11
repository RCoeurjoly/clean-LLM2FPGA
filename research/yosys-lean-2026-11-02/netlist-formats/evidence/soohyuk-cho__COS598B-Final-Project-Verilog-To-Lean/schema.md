# Lean Target Schema for Verilog-to-Lean Translation

This document defines the required Lean output structure for each tier of hardware modules.

---

## Schema A: Combinational Modules (Tier 1)

Combinational modules have no state — they are pure functions from inputs to outputs.
 
### Required structure

```lean
namespace <ModuleName>_<VariantId>

-- Type abbreviations (use BitVec for multi-bit, Bool for single-bit)
abbrev Bit := Bool
abbrev BitsN := BitVec N        -- e.g., Bits4 := BitVec 4

-- 1. Input structure: one field per module input port
structure Inputs where
  <field> : <type>
  ...
deriving Repr, DecidableEq

-- 2. Output structure: one field per module output port
structure Outputs where
  <field> : <type>
  ...
deriving Repr, DecidableEq

-- 3. (Optional) Helper constructors for BitVec literals
def bvN (n : Nat) : BitsN := BitVec.ofNat N n

-- 4. Evaluation function: the combinational logic
def eval (input : Inputs) : Outputs := ...

-- 5. Theorems: concrete test-vector checks or symbolic properties
--    Use `native_decide` for concrete test vectors
--    Use `rfl` when the proof is definitional
theorem <name> : eval { ... } = { ... } := by native_decide

end <ModuleName>_<VariantId>
```

### Rules
- One namespace per module variant
- All structures must derive `Repr` and `DecidableEq`
- The evaluation function must be named `eval`
- No `sorry` allowed
- Theorems should cover representative input cases (at minimum one per branch/case)
- Use `BitVec` for multi-bit signals, `Bool` for single-bit signals

---

## Schema B: Sequential Modules (Tier 2+)

Sequential modules have state that evolves over clock cycles.

### Required structure

```lean
import Std

namespace <ModuleName>

-- Helper definitions (e.g., wrapping arithmetic)
def modulus (w : Nat) : Nat := 2 ^ w
def wrap (w : Nat) (n : Nat) : Nat := n % modulus w

-- 1. State structure: internal registers / flip-flop values
structure State (w : Nat) where      -- parameterize by width if applicable
  <field> : <type>
  ...
deriving Repr, DecidableEq

-- 2. Input structure: all input ports
structure Input where
  rst  : Bool
  en   : Bool
  ...
deriving Repr, DecidableEq

-- 3. Output structure: all output ports
structure Output where
  <field> : <type>
  ...
deriving Repr, DecidableEq

-- 4. Initial/reset state
def init (w : Nat) : State w := { ... }       -- or `zeroState`

-- 5. Transition function: one clock-cycle state update
--    Must respect Verilog priority (e.g., rst > load > en > hold)
def step {w : Nat} (s : State w) (i : Input) : State w := ...

-- 6. Output function: combinational read of state
def out {w : Nat} (s : State w) : Output := ...

-- 7. Theorems with @[simp] attribute
--    Categories:
--    a) Reset behavior:      step under rst = init
--    b) Hold behavior:       step with nothing enabled = identity
--    c) Functional behavior: step under each operation
--    d) Priority:            rst beats load beats en, etc.
--    e) Composition:         multi-step properties (optional)
--    f) Output theorems:     out ∘ step under various inputs

@[simp] theorem step_reset ... := by simp [step, init]
@[simp] theorem step_hold  ... := by simp [step]
@[simp] theorem step_<op>  ... := by simp [step, ...]

end <ModuleName>
```

### Rules
- State must capture all flip-flop / register values from the Verilog module
- `step` must faithfully encode the priority of the Verilog `always` block
- `init` / `zeroState` must match the Verilog reset behavior
- `out` is a pure function from state to outputs (Moore-style)
- Parameterize by width `w` when the Verilog module uses `parameter`
- Use `Nat` with wrapping (`% 2^w`) or `BitVec w` for fixed-width arithmetic
- No `sorry` allowed
- Theorems should cover: reset, hold, each operation, and priority ordering

---

## Schema C: Controllers / FSMs (Tier 3)

Extends Schema B with additional requirements for complex controllers:

- **FSM state encoding**: Use an inductive type for named states
- **Multiple state variables**: State structure may have multiple fields (e.g., pointer, count, memory)
- **Richer properties**: Mutual exclusion of grants, FIFO ordering, fairness
- **Memory modeling**: Use `Array` or `Vector` for register files / buffers

```lean
-- Example: FSM states as an inductive type
inductive FsmState where
  | idle | active | done
deriving Repr, DecidableEq

structure State where
  phase : FsmState
  counter : Nat
  ...
```

---

## Common conventions

| Verilog construct | Lean representation |
|-------------------|---------------------|
| `wire` / `reg` (1-bit) | `Bool` |
| `[N-1:0]` bus | `BitVec N` or `Nat` with wrapping |
| `always @(posedge clk)` | `step` function |
| `always @(*)` | `eval` function (combinational) |
| `parameter WIDTH = N` | Lean type parameter `(w : Nat)` |
| `if/else` priority chain | Nested `if/then/else` in same order |
| `case` statement | `match` expression |
| Reset to 0 | `init` / `zeroState` returns zero-valued state |
