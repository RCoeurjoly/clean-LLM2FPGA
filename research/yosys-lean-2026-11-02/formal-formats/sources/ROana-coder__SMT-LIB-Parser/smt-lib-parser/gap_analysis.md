# Gap Analysis: SMT-LIB Parser vs "SAT SMT by Example"

This document tracks feature parity between our `smt-lib-parser` and the requirements for running examples from the book "SAT SMT by Example" (Dennis Yurichev).

## ✅ Implemented Features

1.  **Core SMT-LIB Structure**
    *   Command parsing & execution structure.
    *   `declare-const`, `declare-fun`, `define-fun`.
    *   `assert`, `check-sat`.

2.  **Solver Interaction**
    *   `get-model`: Parsed & validated.
    *   `get-value`: Parsed & validated.

3.  **Theories**
    *   **Core**: Board `Bool` logic (`and`, `or`, `not`, `=>`, `xor`, `ite`).
    *   **Ints**: Arithmetic (`+`, `-`, `*`, `div`, `mod`) and ordering.
    *   **Strings**: Literals (`"foo"`) and Operations (`str.++`, `str.len`, `str.contains`, etc.).

---

## 🚧 Critical Gaps (Priority High)

These features are used extensively in the book and are currently **completely missing**.

### 1. Fixed-Size Bit-Vectors (`BitVec`)
*   **Usage**: Low-level software modeling, reverse engineering examples, crypto, circuit SAT.
*   **Missing Syntax**:
    *   Sort: `(_ BitVec m)` (e.g., `(_ BitVec 32)`).
    *   Literals: `#b101` (binary), `#x0F` (hex).
*   **Missing Operators**:
    *   Arithmetic: `bvadd`, `bvsub`, `bvmul`, `bvudiv`, `bvsrem`...
    *   Bitwise: `bvand`, `bvor`, `bvxor`, `bvnot`, `bvneg`.
    *   Shift/Rotate: `bvshl`, `bvlshr`, `bvashr`, `rotate_left`...
    *   Manipulation: `concat`, `extract`.
*   **Impact**: Blocks ~60% of technical examples.

### 2. Arrays (`Array`)
*   **Usage**: Modeling memory (RAM/Stack), state machines.
*   **Missing Syntax**:
    *   Sort: `(Array IndexSort ElementSort)`.
*   **Missing Operators**:
    *   `select`: Read from array.
    *   `store`: Write to array (returns new array).
*   **Impact**: Blocks memory modeling examples.

---

## 📉 Minor Gaps (Priority Low)

Features that are either less frequent or can be worked around.

### 3. Incremental Solving
*   **Commands**: `push`, `pop`, `reset`.
*   **Status**: Scoped out by user request. We support validity checking but not state stack manipulation.

### 4. Advanced Logic
*   **Quantifiers**: `forall`, `exists`.
*   **Optimization**: `maximize`, `minimize` (MaxSMT).

## Recommended Roadmap
1.  **Implement BitVectors**: This yields the highest ROI for running book examples.
2.  **Implement Arrays**: Follow-up for memory examples.
