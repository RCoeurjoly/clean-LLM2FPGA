-- Copyright (c) 2026 Kevin Baragona
-- SPDX-License-Identifier: Apache-2.0
import Lean.Data.Json.Parser
import Loom.Hw.ImportIR

/-! # JSON boundary for the neutral import IR

The parser is deliberately explicit and fail-closed. Unknown enum spellings,
missing fields, malformed widths, or malformed source locations are rejected
before `ImportIR.Module.lowerLocalDesign?` performs semantic validation.
-/

namespace Loom.Hw.ImportJson

open Lean
open ImportIR

private def field (json : Json) (name : String) : Except String Json :=
  json.getObjVal? name

private def stringField (json : Json) (name : String) : Except String String := do
  let value ← field json name
  value.getStr?

private def natField (json : Json) (name : String) : Except String Nat := do
  let value ← field json name
  value.getNat?

private def boolField (json : Json) (name : String) : Except String Bool := do
  let value ← field json name
  value.getBool?

private def arrayField (json : Json) (name : String) : Except String (Array Json) := do
  let value ← field json name
  value.getArr?

private def optionalStringField (json : Json) (name : String) : Except String (Option String) := do
  let value ← field json name
  if value.isNull then return none
  return some (← value.getStr?)

private def parseList {α : Type} (parser : Json → Except String α)
    (values : Array Json) : Except String (List α) :=
  values.toList.mapM parser

private def parseLocation (json : Json) : Except String SourceLocation := do
  let file ← stringField json "file"
  let startLine ← natField json "start_line"
  let startColumn ← natField json "start_column"
  let endLine ← natField json "end_line"
  let endColumn ← natField json "end_column"
  return { file, startLine, startColumn, endLine, endColumn }

private def optionalLocationField (json : Json) (name : String) :
    Except String (Option SourceLocation) := do
  let value ← field json name
  if value.isNull then return none
  return some (← parseLocation value)

private def parsePortDirection (value : String) : Except String ImportIR.PortDirection :=
  match value with
  | "input" => pure .input
  | "output" => pure .output
  | "inout" => pure .inout
  | other => throw s!"unknown import port direction '{other}'"

private def parseClockEdge (value : String) : Except String Loom.ClockEdge :=
  match value with
  | "rising" => pure .rising
  | "falling" => pure .falling
  | other => throw s!"unknown clock edge '{other}'"

private def parseResetKind (value : String) : Except String ResetKind :=
  match value with
  | "resetless" => pure .resetless
  | "synchronous" => pure .synchronous
  | "asynchronous" => pure .asynchronous
  | "asynchronous_assert_synchronous_release" =>
      pure .asynchronousAssertSynchronousRelease
  | other => throw s!"unknown reset kind '{other}'"

private def parseUnaryOp (value : String) : Except String UnaryOp :=
  match value with
  | "bit_not" => pure .bitNot
  | "negate" => pure .negate
  | "reduce_bool" => pure .reduceBool
  | "reduce_and" => pure .reduceAnd
  | "logical_not" => pure .logicalNot
  | other => throw s!"unknown unary operation '{other}'"

private def parseBinaryOp (value : String) : Except String BinaryOp :=
  match value with
  | "bit_and" => pure .bitAnd
  | "bit_or" => pure .bitOr
  | "bit_xor" => pure .bitXor
  | "add" => pure .add
  | "sub" => pure .sub
  | "mul" => pure .mul
  | "unsigned_div" => pure .unsignedDiv
  | "unsigned_rem" => pure .unsignedRem
  | "shift_left" => pure .shiftLeft
  | "logical_shift_right" => pure .logicalShiftRight
  | "equal" => pure .equal
  | "unsigned_less_than" => pure .unsignedLessThan
  | "signed_less_than" => pure .signedLessThan
  | other => throw s!"unknown binary operation '{other}'"

private def parsePartialValueClass (value : String) :
    Except String PartialValueClass :=
  match value with
  | "synthesis_dont_care" => pure .synthesisDontCare
  | "unreachable_decode" => pure .unreachableDecode
  | "undriven_behavior" => pure .undrivenBehavior
  | "uninitialized_state_or_memory" => pure .uninitializedStateOrMemory
  | other => throw s!"unknown partial-value classification '{other}'"

private def parsePartialValue (json : Json) : Except String PartialValue := do
  let site ← stringField json "site"
  let valueClass ←
    parsePartialValueClass (← stringField json "classification")
  let knownMask ← natField json "known_mask"
  let knownValue ← natField json "known_value"
  let implementationValue ← natField json "implementation_value"
  let rationale ← stringField json "rationale"
  return PartialValue.mk site valueClass knownMask knownValue
    implementationValue rationale

private def optionalPartialValueField (json : Json) (name : String) :
    Except String (Option PartialValue) := do
  match json.getObjVal? name with
  | .error _ => return none
  | .ok value =>
      if value.isNull then return none
      return some (← parsePartialValue value)

private partial def parseExpr (json : Json) : Except String ImportIR.Expr := do
  let kind ← stringField json "kind"
  let width ← natField json "width"
  let source ← parseLocation (← field json "source")
  match kind with
  | "literal" => return .literal width (← natField json "value") source
  | "partial_literal" =>
      return .partialLiteral width
        (← parsePartialValue (← field json "partial")) source
  | "signal" => return .signal width (← stringField json "name") source
  | "unary" =>
      return .unary width (← parseUnaryOp (← stringField json "op"))
        (← parseExpr (← field json "value")) source
  | "binary" =>
      return .binary width (← parseBinaryOp (← stringField json "op"))
        (← parseExpr (← field json "left"))
        (← parseExpr (← field json "right")) source
  | "mux" =>
      return .mux width
        (← parseExpr (← field json "condition"))
        (← parseExpr (← field json "yes"))
        (← parseExpr (← field json "no")) source
  | "slice" =>
      return .slice width (← parseExpr (← field json "value"))
        (← natField json "offset") source
  | "zero_extend" =>
      return .zeroExtend width (← parseExpr (← field json "value")) source
  | "sign_extend" =>
      return .signExtend width (← parseExpr (← field json "value")) source
  | "concat" =>
      return .concat width (← parseExpr (← field json "high"))
        (← parseExpr (← field json "low")) source
  | "memory_read" =>
      return .memoryRead width (← stringField json "memory")
        (← parseExpr (← field json "address")) source
  | other => throw s!"unknown import expression kind '{other}'"

private def parsePort (json : Json) : Except String ImportIR.Port := do
  let name ← stringField json "name"
  let direction ← parsePortDirection (← stringField json "direction")
  let width ← natField json "width"
  let semanticType ← stringField json "semantic_type"
  let source ← parseLocation (← field json "source")
  return { name, direction, width, semanticType, source }

private def parseReset (json : Json) : Except String Reset := do
  let kind ← parseResetKind (← stringField json "kind")
  let port ← optionalStringField json "port"
  let activeHigh ← boolField json "active_high"
  let source ← optionalLocationField json "source"
  return { kind, port, activeHigh, source }

private def parseDomain (json : Json) : Except String ImportIR.ClockDomain := do
  let name ← stringField json "name"
  let clockPort ← stringField json "clock_port"
  let edge ← parseClockEdge (← stringField json "edge")
  let reset ← parseReset (← field json "reset")
  let source ← parseLocation (← field json "source")
  return { name, clockPort, edge, reset, source }

private def parseRegister (json : Json) : Except String Register := do
  let name ← stringField json "name"
  let width ← natField json "width"
  let init ← natField json "init"
  let next ← parseExpr (← field json "next")
  let domain ← match json.getObjVal? "domain" with
    | .error _ => pure none
    | .ok value => if value.isNull then pure none else some <$> value.getStr?
  let source ← parseLocation (← field json "source")
  return { name, width, init, next, domain, source }

private def parseMemoryWrite (json : Json) : Except String MemoryWrite := do
  let port ← natField json "port"
  let enable ← parseExpr (← field json "enable")
  let address ← parseExpr (← field json "address")
  let data ← parseExpr (← field json "data")
  let source ← parseLocation (← field json "source")
  return { port, enable, address, data, source }

private def parseMemory (json : Json) : Except String Memory := do
  let name ← stringField json "name"
  let addressWidth ← natField json "address_width"
  let dataWidth ← natField json "data_width"
  let init ← parseList Json.getNat? (← arrayField json "init")
  let initRefinement ← optionalPartialValueField json "init_refinement"
  let writes ← parseList parseMemoryWrite (← arrayField json "writes")
  let source ← parseLocation (← field json "source")
  return { name, addressWidth, dataWidth, init, initRefinement, writes, source }

private def parseOutput (json : Json) : Except String Output := do
  let name ← stringField json "name"
  let width ← natField json "width"
  let value ← parseExpr (← field json "value")
  let source ← parseLocation (← field json "source")
  return { name, width, value, source }

private def parseParameter (json : Json) : Except String (String × String) := do
  return (← stringField json "name", ← stringField json "value")

private def parseInstanceConnection (json : Json) : Except String InstanceConnection := do
  let port ← stringField json "port"
  let direction ← parsePortDirection (← stringField json "direction")
  let signal ← stringField json "signal"
  let width ← natField json "width"
  let valueJson ← field json "value"
  let value ← if valueJson.isNull then pure none else some <$> parseExpr valueJson
  let source ← parseLocation (← field json "source")
  return { port, direction, signal, width, value, source }

private def parseInstance (json : Json) : Except String Instance := do
  let name ← stringField json "name"
  let moduleName ← stringField json "module_name"
  let parameters ← parseList parseParameter (← arrayField json "parameters")
  let connections ← parseList parseInstanceConnection (← arrayField json "connections")
  let source ← parseLocation (← field json "source")
  return { name, moduleName, parameters, connections, source }

private def parseUnsupported (json : Json) : Except String UnsupportedConstruct := do
  let kind ← stringField json "kind"
  let detail ← stringField json "detail"
  let source ← parseLocation (← field json "source")
  return { kind, detail, source }

private def expressionRef (known : Array ImportIR.Expr) (json : Json)
    (name : String) : Except String ImportIR.Expr := do
  let reference ← natField json name
  let some expression := known[reference]?
    | throw s!"expression reference {reference} is forward or outside the module table"
  return expression

private def parseExprNode (known : Array ImportIR.Expr) (json : Json) :
    Except String ImportIR.Expr := do
  let kind ← stringField json "kind"
  let width ← natField json "width"
  let source ← parseLocation (← field json "source")
  match kind with
  | "literal" => return .literal width (← natField json "value") source
  | "partial_literal" =>
      return .partialLiteral width
        (← parsePartialValue (← field json "partial")) source
  | "signal" => return .signal width (← stringField json "name") source
  | "unary" =>
      return .unary width (← parseUnaryOp (← stringField json "op"))
        (← expressionRef known json "value") source
  | "binary" =>
      return .binary width (← parseBinaryOp (← stringField json "op"))
        (← expressionRef known json "left")
        (← expressionRef known json "right") source
  | "mux" =>
      return .mux width (← expressionRef known json "condition")
        (← expressionRef known json "yes")
        (← expressionRef known json "no") source
  | "slice" =>
      return .slice width (← expressionRef known json "value")
        (← natField json "offset") source
  | "zero_extend" =>
      return .zeroExtend width (← expressionRef known json "value") source
  | "sign_extend" =>
      return .signExtend width (← expressionRef known json "value") source
  | "concat" =>
      return .concat width (← expressionRef known json "high")
        (← expressionRef known json "low") source
  | "memory_read" =>
      return .memoryRead width (← stringField json "memory")
        (← expressionRef known json "address") source
  | other => throw s!"unknown import expression kind '{other}'"

private def parseExprTable (values : Array Json) :
    Except String (Array ImportIR.Expr) := do
  let mut known := #[]
  for value in values do
    known := known.push (← parseExprNode known value)
  return known

private def parseRegisterRef (known : Array ImportIR.Expr) (json : Json) :
    Except String Register := do
  let name ← stringField json "name"
  let width ← natField json "width"
  let init ← natField json "init"
  let next ← expressionRef known json "next"
  let domain ← match json.getObjVal? "domain" with
    | .error _ => pure none
    | .ok value => if value.isNull then pure none else some <$> value.getStr?
  let source ← parseLocation (← field json "source")
  return { name, width, init, next, domain, source }

private def parseMemoryWriteRef (known : Array ImportIR.Expr) (json : Json) :
    Except String MemoryWrite := do
  let port ← natField json "port"
  let enable ← expressionRef known json "enable"
  let address ← expressionRef known json "address"
  let data ← expressionRef known json "data"
  let source ← parseLocation (← field json "source")
  return { port, enable, address, data, source }

private def parseMemoryRef (known : Array ImportIR.Expr) (json : Json) :
    Except String Memory := do
  let name ← stringField json "name"
  let addressWidth ← natField json "address_width"
  let dataWidth ← natField json "data_width"
  let init ← parseList Json.getNat? (← arrayField json "init")
  let initRefinement ← optionalPartialValueField json "init_refinement"
  let writes ← parseList (parseMemoryWriteRef known) (← arrayField json "writes")
  let source ← parseLocation (← field json "source")
  return { name, addressWidth, dataWidth, init, initRefinement, writes, source }

private def parseOutputRef (known : Array ImportIR.Expr) (json : Json) :
    Except String Output := do
  let name ← stringField json "name"
  let width ← natField json "width"
  let value ← expressionRef known json "value"
  let source ← parseLocation (← field json "source")
  return { name, width, value, source }

private def parseInstanceConnectionRef (known : Array ImportIR.Expr)
    (json : Json) : Except String InstanceConnection := do
  let port ← stringField json "port"
  let direction ← parsePortDirection (← stringField json "direction")
  let signal ← stringField json "signal"
  let width ← natField json "width"
  let valueJson ← field json "value"
  let value ← if valueJson.isNull then pure none else
    some <$> expressionRef known json "value"
  let source ← parseLocation (← field json "source")
  return { port, direction, signal, width, value, source }

private def parseInstanceRef (known : Array ImportIR.Expr) (json : Json) :
    Except String Instance := do
  let name ← stringField json "name"
  let moduleName ← stringField json "module_name"
  let parameters ← parseList parseParameter (← arrayField json "parameters")
  let connections ← parseList (parseInstanceConnectionRef known)
    (← arrayField json "connections")
  let source ← parseLocation (← field json "source")
  return { name, moduleName, parameters, connections, source }

private def parseModuleDagJson (json : Json) : Except String ImportIR.Module := do
  let known ← parseExprTable (← arrayField json "expressions")
  let name ← stringField json "name"
  let ports ← parseList parsePort (← arrayField json "ports")
  let domains ← parseList parseDomain (← arrayField json "domains")
  let registers ← parseList (parseRegisterRef known) (← arrayField json "registers")
  let memories ← parseList (parseMemoryRef known) (← arrayField json "memories")
  let outputs ← parseList (parseOutputRef known) (← arrayField json "outputs")
  let instances ← parseList (parseInstanceRef known) (← arrayField json "instances")
  let unsupported ← parseList parseUnsupported (← arrayField json "unsupported")
  let source ← parseLocation (← field json "source")
  return ImportIR.Module.mk name ports domains registers memories outputs
    instances unsupported source

def parseModuleJson (json : Json) : Except String ImportIR.Module := do
  let name ← stringField json "name"
  let ports ← parseList parsePort (← arrayField json "ports")
  let domains ← parseList parseDomain (← arrayField json "domains")
  let registers ← parseList parseRegister (← arrayField json "registers")
  let memories ← parseList parseMemory (← arrayField json "memories")
  let outputs ← parseList parseOutput (← arrayField json "outputs")
  let instances ← parseList parseInstance (← arrayField json "instances")
  let unsupported ← parseList parseUnsupported (← arrayField json "unsupported")
  let source ← parseLocation (← field json "source")
  return ImportIR.Module.mk name ports domains registers memories outputs
    instances unsupported source

/-- Parse the complete adapter document and return its neutral module.  The
frontend provenance envelope remains in the exact JSON artifact used by the
signoff layer; semantic lowering consumes only the neutral `module` field. -/
def parseDocument (text : String) : Except String ImportIR.Module := do
  let json ← Json.parse text
  unless (← natField json "schema") == 1 do
    throw "unsupported neutral import JSON schema"
  parseModuleJson (← field json "module")

private def parsePackageJson (json : Json) : Except String ImportIR.Package := do
  let top ← stringField json "top"
  let modules ← parseList parseModuleDagJson (← arrayField json "modules")
  let source ← parseLocation (← field json "source")
  return { top, modules, source }

/-- Parse a schema-v2 closed elaborated hierarchy. Structural acceptance is
separate (`Package.check?`) so callers cannot confuse parsing with checking. -/
def parsePackageDocument (text : String) : Except String ImportIR.Package := do
  let json ← Json.parse text
  unless (← natField json "schema") == 2 do
    throw "unsupported neutral import package JSON schema"
  parsePackageJson (← field json "package")

end Loom.Hw.ImportJson
