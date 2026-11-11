/-
  Verification.lean
  =================
  Formal proofs for the SMT-LIB Parser & Evaluator
  (Verified with typed operators)
-/

import SmtLib

open SmtLib

-- 1. Simple Evaluation Proof
--    Verify that (1 + 1) evaluates to 2
theorem eval_add_one_one :
  termToInt (Term.app Op.plus [Term.intLit 1, Term.intLit 1]) = some 2 :=
by
  rfl -- computation by reflection

-- 2. Term Evaluation Proof
--    Verify that (1 + 1 = 2) evaluates to true
theorem eval_add_eq :
  evalTerm (Term.app Op.eq [Term.app Op.plus [Term.intLit 1, Term.intLit 1], Term.intLit 2]) = some true :=
by
  native_decide

-- 3. Proving 'theorem asdf' (Specification Validation)
--    This was the failing theorem in Main.lean

def commands := [
  SmtLib.Command.defineFun
    "inc"
    [("x", SmtLib.Srt.int)]
    (SmtLib.Srt.int)
    (SmtLib.Term.app Op.plus [SmtLib.Term.var "x", SmtLib.Term.intLit 1]),

  SmtLib.Command.assert
    (SmtLib.Term.app Op.eq [SmtLib.Term.app (Op.custom "inc") [SmtLib.Term.intLit 10], SmtLib.Term.intLit 11]),

  SmtLib.Command.checkSat
]


-- Prove it formally (computationally)
theorem spec_asdf : checkProblem commands = true :=
by
  native_decide

/- ==========================================
   ARITHMETIC EVALUATION PROPERTIES
   ========================================== -/

-- Subtraction is correct
theorem eval_sub :
  termToInt (Term.app Op.minus [Term.intLit 10, Term.intLit 3]) = some 7 := by native_decide

-- Multiplication is correct
theorem eval_mul :
  termToInt (Term.app Op.mul [Term.intLit 4, Term.intLit 5]) = some 20 := by native_decide

-- Nested arithmetic
theorem eval_nested_arith :
  termToInt (Term.app Op.plus [Term.app Op.mul [Term.intLit 2, Term.intLit 3], Term.intLit 4]) = some 10 := by native_decide

-- Division is correct
theorem eval_div :
  termToInt (Term.app Op.div [Term.intLit 20, Term.intLit 4]) = some 5 := by native_decide

-- Modulo is correct
theorem eval_mod :
  termToInt (Term.app Op.mod [Term.intLit 17, Term.intLit 5]) = some 2 := by native_decide

-- Negation is correct
theorem eval_neg :
  termToInt (Term.app Op.minus [Term.intLit 42]) = some (-42) := by native_decide

/- ==========================================
   BOOLEAN/COMPARISON EVALUATION
   ========================================== -/

-- Less-than works (true case)
theorem eval_lt_true :
  evalTerm (Term.app Op.lt [Term.intLit 5, Term.intLit 10]) = some true := by native_decide

-- Less-than works (false case)
theorem eval_lt_false :
  evalTerm (Term.app Op.lt [Term.intLit 10, Term.intLit 5]) = some false := by native_decide

-- Greater-than works (true case)
theorem eval_gt_true :
  evalTerm (Term.app Op.gt [Term.intLit 10, Term.intLit 5]) = some true := by native_decide

-- Greater-than works (false case)
theorem eval_gt_false :
  evalTerm (Term.app Op.gt [Term.intLit 5, Term.intLit 10]) = some false := by native_decide

-- Less-than-or-equal works
theorem eval_le_true :
  evalTerm (Term.app Op.le [Term.intLit 5, Term.intLit 5]) = some true := by native_decide

-- Greater-than-or-equal works
theorem eval_ge_true :
  evalTerm (Term.app Op.ge [Term.intLit 10, Term.intLit 10]) = some true := by native_decide

-- Logical AND (both true)
theorem eval_and_true :
  evalTerm (Term.app Op.and [Term.app Op.lt [Term.intLit 1, Term.intLit 2], Term.app Op.gt [Term.intLit 3, Term.intLit 0]]) = some true := by native_decide

-- Logical OR (one false)
theorem eval_or_true :
  evalTerm (Term.app Op.or [Term.app Op.lt [Term.intLit 10, Term.intLit 5], Term.app Op.gt [Term.intLit 3, Term.intLit 0]]) = some true := by native_decide

-- Logical NOT
theorem eval_not :
  evalTerm (Term.app Op.not [Term.app Op.lt [Term.intLit 10, Term.intLit 5]]) = some true := by native_decide

-- Implication (true => true = true)
theorem eval_imp_true :
  evalTerm (Term.app Op.imp [Term.app Op.gt [Term.intLit 5, Term.intLit 0], Term.app Op.lt [Term.intLit 1, Term.intLit 10]]) = some true := by native_decide

/- ==========================================
   FUNCTION EXPANSION CORRECTNESS
   ========================================== -/

-- Note: Direct Term equality proofs require DecidableEq for Term, which is complex.
-- We verify function expansion indirectly via eval_double_func below.

-- Function evaluation end-to-end
def doubleCommands := [
  Command.defineFun "double" [("x", Srt.int)] Srt.int (Term.app Op.plus [Term.var "x", Term.var "x"]),
  Command.assert (Term.app Op.eq [Term.app (Op.custom "double") [Term.intLit 7], Term.intLit 14])
]

theorem eval_double_func :
  checkProblem doubleCommands = true := by native_decide

-- This ensures that 'expand' correctly processes arguments (args') before substitution
def nestedCommands := [
  Command.defineFun "inc" [("x", Srt.int)] Srt.int (Term.app Op.plus [Term.var "x", Term.intLit 1]),
  -- assert inc(inc(10)) = 12
  Command.assert (Term.app Op.eq [
    Term.app (Op.custom "inc") [Term.app (Op.custom "inc") [Term.intLit 10]],
    Term.intLit 12
  ])
]

theorem eval_nested_func :
  checkProblem nestedCommands = true := by native_decide

/- ==========================================
   PARSER ROUND-TRIP PROPERTIES
   ========================================== -/

-- Parsing a valid script produces a Problem
theorem parse_valid_script :
  (parse "(assert (> 10 5))").isSome = true := by native_decide

-- Parsing check-sat succeeds
theorem parse_check_sat :
  (parse "(check-sat)").isSome = true := by native_decide

-- Parsing declare-const succeeds
theorem parse_declare_const :
  (parse "(declare-const x Int)").isSome = true := by native_decide

/- ==========================================
   STRUCTURAL PROPERTIES
   ========================================== -/

-- Empty list of commands passes checkProblem
theorem empty_commands_true :
  checkProblem [] = true := by native_decide

-- Single assert with true condition evaluates to true
theorem assert_true :
  checkProblem [Command.assert (Term.app Op.gt [Term.intLit 10, Term.intLit 5])] = true := by native_decide

-- Single assert with false condition evaluates to false
theorem assert_false :
  checkProblem [Command.assert (Term.app Op.lt [Term.intLit 10, Term.intLit 5])] = false := by native_decide

-- Multiple true asserts all pass
theorem multiple_asserts_true :
  checkProblem [
    Command.assert (Term.app Op.gt [Term.intLit 10, Term.intLit 5]),
    Command.assert (Term.app Op.lt [Term.intLit 1, Term.intLit 2]),
    Command.assert (Term.app Op.eq [Term.intLit 3, Term.intLit 3])
  ] = true := by native_decide

-- One false assert makes the whole problem false
theorem one_false_assert :
  checkProblem [
    Command.assert (Term.app Op.gt [Term.intLit 10, Term.intLit 5]),
    Command.assert (Term.app Op.lt [Term.intLit 100, Term.intLit 1])  -- false
  ] = false := by native_decide
