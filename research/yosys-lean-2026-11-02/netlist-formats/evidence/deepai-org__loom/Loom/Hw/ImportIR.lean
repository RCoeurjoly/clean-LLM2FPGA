-- Copyright (c) 2026 Kevin Baragona
-- SPDX-License-Identifier: Apache-2.0
import Loom.Hw.Stateless
import Loom.Clock
import Loom.Artifact

/-!
# Neutral, module-preserving hardware import IR

This IR is deliberately independent of Verilog, Yosys, and any one parser.
Frontends populate source-located data; the checked lowering below accepts
only constructs whose semantics map exactly onto an ordinary Loom `Design`.
Unsupported constructs fail closed.  Successful lowering does **not** prove
that an untrusted frontend interpreted its source language correctly; exact
source/artifact identities and external equivalence evidence remain required.
-/

namespace Loom.Hw.ImportIR

structure SourceLocation where
  file : String
  startLine : Nat
  startColumn : Nat := 0
  endLine : Nat
  endColumn : Nat := 0
  deriving Repr, DecidableEq, BEq

namespace SourceLocation

def render (location : SourceLocation) : String :=
  s!"{location.file}:{location.startLine}:{location.startColumn}-{location.endLine}:{location.endColumn}"

def validB (location : SourceLocation) : Bool :=
  !location.file.isEmpty && location.startLine > 0 &&
    location.endLine ≥ location.startLine

end SourceLocation

inductive PortDirection where
  | input
  | output
  | inout
  deriving Repr, DecidableEq, BEq

structure Port where
  name : String
  direction : PortDirection
  width : Nat
  semanticType : String := "bits"
  source : SourceLocation
  deriving Repr, DecidableEq, BEq

inductive ResetKind where
  | resetless
  | synchronous
  | asynchronous
  | asynchronousAssertSynchronousRelease
  deriving Repr, DecidableEq, BEq

structure Reset where
  kind : ResetKind
  port : Option String := none
  activeHigh : Bool := true
  source : Option SourceLocation := none
  deriving Repr, DecidableEq, BEq

structure ClockDomain where
  name : String
  clockPort : String
  edge : Loom.ClockEdge
  reset : Reset := { kind := .resetless }
  source : SourceLocation
  deriving Repr, DecidableEq, BEq

inductive UnaryOp where
  | bitNot
  | negate
  /-- One iff at least one operand bit is set. -/
  | reduceBool
  /-- One iff every operand bit is set. -/
  | reduceAnd
  /-- One iff the operand is zero. -/
  | logicalNot
  deriving Repr, DecidableEq, BEq

inductive BinaryOp where
  | bitAnd
  | bitOr
  | bitXor
  | add
  | sub
  | mul
  | unsignedDiv
  | unsignedRem
  | shiftLeft
  | logicalShiftRight
  | equal
  | unsignedLessThan
  | signedLessThan
  deriving Repr, DecidableEq, BEq

/-- Why a four-state source value may be refined to concrete two-state RTL. -/
inductive PartialValueClass where
  | synthesisDontCare
  | unreachableDecode
  | undrivenBehavior
  | uninitializedStateOrMemory
  deriving Repr, DecidableEq, BEq

/-- One explicit implementation choice for a partially specified source
constant. `knownMask` selects source-known bits. -/
structure PartialValue where
  site : String
  classification : PartialValueClass
  knownMask : Nat
  knownValue : Nat
  implementationValue : Nat
  rationale : String
  deriving Repr, DecidableEq

namespace PartialValue

def allowedB (choice : PartialValue) (width candidate : Nat) : Bool :=
  candidate < 2 ^ width &&
    (candidate &&& choice.knownMask) ==
      (choice.knownValue &&& choice.knownMask)

def validB (choice : PartialValue) (width : Nat) : Bool :=
  (!choice.site.isEmpty && !choice.rationale.isEmpty && width > 0 &&
    choice.knownMask < 2 ^ width && choice.knownValue < 2 ^ width) &&
    choice.allowedB width choice.implementationValue

/-- A checked implementation is a member of the set of concrete values
allowed by the source-known bits. -/
theorem implementation_allowed {choice : PartialValue} {width : Nat}
    (valid : choice.validB width = true) :
    choice.allowedB width choice.implementationValue = true := by
  exact (Bool.and_eq_true_iff.mp valid).2

end PartialValue

/-- Widths remain explicit at the import boundary.  This is intentionally a
first-order serializable tree rather than Lean's intrinsically indexed
`Hw.Expr`; `lowerExpr?` is the checked dependent-typing boundary. -/
inductive Expr where
  | literal (width value : Nat) (source : SourceLocation)
  | partialLiteral (width : Nat) (choice : PartialValue)
      (source : SourceLocation)
  | signal (width : Nat) (name : String) (source : SourceLocation)
  | unary (width : Nat) (op : UnaryOp) (value : Expr) (source : SourceLocation)
  | binary (width : Nat) (op : BinaryOp) (left right : Expr)
      (source : SourceLocation)
  | mux (width : Nat) (condition yes no : Expr) (source : SourceLocation)
  | slice (width : Nat) (value : Expr) (offset : Nat) (source : SourceLocation)
  | zeroExtend (width : Nat) (value : Expr) (source : SourceLocation)
  | signExtend (width : Nat) (value : Expr) (source : SourceLocation)
  | concat (width : Nat) (high low : Expr) (source : SourceLocation)
  | memoryRead (width : Nat) (memory : String) (address : Expr)
      (source : SourceLocation)
  deriving Repr, DecidableEq

namespace Expr

def width : Expr → Nat
  | .literal width .. | .partialLiteral width .. |
      .signal width .. | .unary width ..
  | .binary width .. | .mux width .. | .slice width ..
  | .zeroExtend width .. | .signExtend width .. | .concat width ..
  | .memoryRead width .. => width

def source : Expr → SourceLocation
  | .literal _ _ source | .partialLiteral _ _ source |
      .signal _ _ source | .unary _ _ _ source
  | .binary _ _ _ _ source | .mux _ _ _ _ source | .slice _ _ _ source
  | .zeroExtend _ _ source | .signExtend _ _ source | .concat _ _ _ source
  | .memoryRead _ _ _ source => source

end Expr

structure Register where
  name : String
  width : Nat
  init : Nat
  next : Expr
  /-- Domain ownership is required only when a source module has multiple
  clock/edge domains. Single-domain imports retain `none` for compatibility. -/
  domain : Option String := none
  source : SourceLocation
  deriving Repr, DecidableEq

structure MemoryWrite where
  port : Nat
  enable : Expr
  address : Expr
  data : Expr
  source : SourceLocation
  deriving Repr, DecidableEq

structure Memory where
  name : String
  addressWidth : Nat
  dataWidth : Nat
  init : List Nat := []
  /-- When source memory initialization contains X/Z bits, this records the
  reviewed two-state implementation refinement for the complete packed image
  (address zero occupies the least-significant `dataWidth` bits). -/
  initRefinement : Option PartialValue := none
  writes : List MemoryWrite := []
  source : SourceLocation
  deriving Repr, DecidableEq

structure Output where
  name : String
  width : Nat
  value : Expr
  source : SourceLocation
  deriving Repr, DecidableEq

structure InstanceConnection where
  port : String
  direction : PortDirection
  signal : String
  width : Nat
  /-- A child input is driven by this exact parent-side expression. Child
  outputs have no value: `signal` names the unique symbolic net they drive. -/
  value : Option Expr := none
  source : SourceLocation
  deriving Repr, DecidableEq

structure Instance where
  name : String
  moduleName : String
  parameters : List (String × String) := []
  connections : List InstanceConnection
  source : SourceLocation
  deriving Repr, DecidableEq

structure UnsupportedConstruct where
  kind : String
  detail : String
  source : SourceLocation
  deriving Repr, DecidableEq, BEq

/-- One recognizable source module. `instances` are retained rather than
flattened; `lowerLocalDesign?` handles the module-owned logic and hierarchy
assembly handles child instances separately. -/
structure Module where
  name : String
  ports : List Port
  domains : List ClockDomain
  registers : List Register
  memories : List Memory
  outputs : List Output
  instances : List Instance := []
  unsupported : List UnsupportedConstruct := []
  source : SourceLocation
  deriving Repr, DecidableEq

/-- A closed inventory of elaborated modules. Module names are source names;
backend-safe HDL names are assigned only at the emission boundary. -/
structure Package where
  top : String
  modules : List Module
  source : SourceLocation
  deriving Repr, DecidableEq

namespace Expr

/-- Signal leaves used by a parent-side binding expression. Duplicates are
irrelevant to dependency checking and are removed deterministically. -/
def signals : Expr → List String
  | .literal .. | .partialLiteral .. => []
  | .signal _ name _ => [name]
  | .unary _ _ value _ | .slice _ value _ _ |
      .zeroExtend _ value _ | .signExtend _ value _ => value.signals
  | .binary _ _ left right _ | .concat _ left right _ =>
      (left.signals ++ right.signals).eraseDups
  | .mux _ condition yes no _ =>
      (condition.signals ++ yes.signals ++ no.signals).eraseDups
  | .memoryRead _ _ address _ => address.signals

end Expr

namespace Package

private def findModule? (package : Package) (name : String) : Option Module :=
  package.modules.find? (·.name == name)

private def connectionShapeOk (child : Module)
    (connection : InstanceConnection) : Bool :=
  child.ports.any fun port =>
    port.name == connection.port && port.direction == connection.direction &&
      port.width == connection.width &&
      match connection.direction, connection.value with
      | .input, some value => value.width == connection.width
      | .output, none => true
      | _, _ => false

private def instanceValidB (package : Package) (inst : Instance) : Bool :=
  match package.findModule? inst.moduleName with
  | none => false
  | some child =>
      !inst.name.isEmpty && inst.source.validB &&
        inst.parameters.isEmpty &&
        Inventory.uniqueB (inst.connections.map (·.port)) &&
        Inventory.uniqueB (inst.connections.map (·.signal)) &&
        inst.connections.all (fun connection =>
          !connection.signal.isEmpty && connection.width > 0 &&
            connection.source.validB && connectionShapeOk child connection) &&
        (child.ports.filter (·.direction == .input)).all fun port =>
          inst.connections.any fun connection =>
            connection.port == port.name && connection.direction == port.direction &&
              connection.width == port.width

private abbrev BoundarySummary := String × List (String × String)

private def findSummary? (summaries : List BoundarySummary) (name : String) :
    Option (List (String × String)) :=
  (summaries.find? (fun summary => summary.1 == name)).map (·.2)

private def reachableNodes : Nat → List (String × String) →
    List String → List String
  | 0, _, reached => reached
  | fuel + 1, edges, reached =>
      let next := (reached ++ edges.filterMap (fun edge =>
        if reached.contains edge.1 then some edge.2 else none)).eraseDups
      if next.length == reached.length then reached
      else reachableNodes fuel edges next

private def reachableB (edges : List (String × String))
    (source sink : String) : Bool :=
  (reachableNodes (edges.length + 1) edges [source]).contains sink

private def moduleDependencyEdges (summaries : List BoundarySummary)
    (module : Module) : List (String × String) :=
  module.instances.flatMap fun inst =>
    let parentEdges := inst.connections.flatMap fun connection =>
      match connection.direction, connection.value with
      | .input, some value => value.signals.map (·, connection.signal)
      | _, _ => []
    let childEdges := match findSummary? summaries inst.moduleName with
      | none => []
      | some dependencies => dependencies.filterMap fun dependency =>
          let input := inst.connections.find? fun connection =>
            connection.port == dependency.1 && connection.direction == .input
          let output := inst.connections.find? fun connection =>
            connection.port == dependency.2 && connection.direction == .output
          match input, output with
          | some input, some output => some (input.signal, output.signal)
          | _, _ => none
    parentEdges ++ childEdges

private def summarizeModule (summaries : List BoundarySummary)
    (module : Module) : BoundarySummary :=
  let edges := moduleDependencyEdges summaries module
  let inputs := module.ports.filter (·.direction == .input)
  let dependencies := module.outputs.flatMap fun output =>
    inputs.filterMap fun input =>
      if output.value.signals.any fun signal =>
          reachableB edges input.name signal then
        some (input.name, output.name)
      else none
  (module.name, dependencies.eraseDups)

private def summarizeLoop : Nat → List Module →
    List BoundarySummary → Option (List BoundarySummary)
  | 0, remaining, summaries =>
      if remaining.isEmpty then some summaries else none
  | fuel + 1, remaining, summaries =>
      if remaining.isEmpty then some summaries
      else
        match remaining.find? fun module =>
            module.instances.all fun inst =>
              (findSummary? summaries inst.moduleName).isSome with
        | none => none
        | some module =>
            -- Package module names are required unique by `validB`; remove the
            -- completed node by that key instead of invoking the derived
            -- structural equality over its potentially very large expression
            -- DAG.  The latter made hierarchy checking proportional to RTL
            -- contents even though only inventory identity matters here.
            summarizeLoop fuel (remaining.filter (·.name != module.name))
              (summaries ++ [summarizeModule summaries module])

private def boundarySummaries? (package : Package) :
    Option (List BoundarySummary) :=
  summarizeLoop (package.modules.length + 1) package.modules []

private def moduleValidB (package : Package)
    (summaries : List BoundarySummary) (module : Module) : Bool :=
  module.source.validB && !module.name.isEmpty &&
    Inventory.uniqueB (module.instances.map (·.name)) &&
    Inventory.uniqueB (module.instances.flatMap fun inst =>
      inst.connections.map (·.signal)) &&
    module.instances.all package.instanceValidB &&
    let edges := moduleDependencyEdges summaries module
    ComponentGraph.topologicalOrderCheckB edges
      (ComponentGraph.proposeTopologicalOrder edges)

/-- Trusted structural acceptance for an elaborated hierarchy. It rejects
missing children, missing child inputs, duplicate/mistyped bindings, shared
symbolic nets, inout boundaries, and same-cycle cycles. Unconsumed child
outputs may be absent exactly as named-port HDL permits. -/
def validB (package : Package) : Bool :=
  let hierarchyEdges := package.modules.flatMap fun module =>
    module.instances.map fun inst => (module.name, inst.moduleName)
  package.source.validB && !package.top.isEmpty && !package.modules.isEmpty &&
    Inventory.uniqueB (package.modules.map (·.name)) &&
    package.modules.any (·.name == package.top) &&
    ComponentGraph.topologicalOrderCheckB hierarchyEdges
      (ComponentGraph.proposeTopologicalOrder hierarchyEdges) &&
    match package.boundarySummaries? with
    | none => false
    | some summaries => package.modules.all (fun module =>
        !module.ports.any (·.direction == .inout) &&
          package.moduleValidB summaries module)

def check? (package : Package) : Except String Package := do
  unless package.validB do
    throw s!"{package.source.render}: invalid hierarchy: missing child/top, duplicate inventory/net/port, direction/width/value mismatch, inout, or combinational cycle"
  return package

end Package

/-- Exact byte identity supplied by an external frontend adapter.  The
frontend's digest is diagnostic; `identity` remains collision-free inside
Loom because it retains the bytes themselves. -/
structure ArtifactBinding where
  role : String
  path : String
  sha256 : String
  identity : Loom.Artifact.Identity

structure ImportManifest where
  schema : Nat := 1
  frontend : String
  version : String
  invocation : List String
  sources : List ArtifactBinding
  neutralArtifact : ArtifactBinding
  assumptions : List String

namespace ImportManifest

private def sha256Like (digest : String) : Bool :=
  digest.length == 64 && digest.toList.all fun c =>
    c.isDigit || ('a' ≤ c && c ≤ 'f') || ('A' ≤ c && c ≤ 'F')

def validB (manifest : ImportManifest) : Bool :=
  manifest.schema == 1 && !manifest.frontend.isEmpty &&
    !manifest.version.isEmpty && !manifest.invocation.isEmpty &&
    !manifest.sources.isEmpty &&
    Inventory.uniqueB (manifest.sources.map (·.path)) &&
    manifest.sources.all (fun artifact =>
      artifact.role == "source" && !artifact.path.isEmpty &&
        sha256Like artifact.sha256 && artifact.identity.byteCount > 0) &&
    manifest.neutralArtifact.role == "neutral_import_ir" &&
    !manifest.neutralArtifact.path.isEmpty &&
    sha256Like manifest.neutralArtifact.sha256 &&
    manifest.neutralArtifact.identity.byteCount > 0 &&
    !manifest.assumptions.isEmpty

end ImportManifest

structure LoweredExpr where
  width : Nat
  value : Loom.Hw.Expr width

private def Module.expressionRoots (module : Module) : List Expr :=
  module.registers.map (·.next) ++
    module.memories.flatMap (fun memory => memory.writes.flatMap fun write =>
      [write.enable, write.address, write.data]) ++
    module.outputs.map (·.value)

private def Expr.readsDeclaredB (regWidths memWidths : Std.HashMap String Nat) :
    Expr → Bool
  | .literal .. | .partialLiteral .. => true
  | .signal width name _ => regWidths.get? name == some width
  | .unary _ _ value _ | .slice _ value _ _ |
      .zeroExtend _ value _ | .signExtend _ value _ =>
      value.readsDeclaredB regWidths memWidths
  | .binary _ _ left right _ | .concat _ left right _ =>
      left.readsDeclaredB regWidths memWidths &&
        right.readsDeclaredB regWidths memWidths
  | .mux _ condition yes no _ =>
      condition.readsDeclaredB regWidths memWidths &&
        yes.readsDeclaredB regWidths memWidths &&
        no.readsDeclaredB regWidths memWidths
  | .memoryRead width memory address _ =>
      memWidths.get? memory == some width &&
        address.readsDeclaredB regWidths memWidths

def Module.sourceReadsDeclaredB (module : Module) : Bool :=
  let regWidths := module.ports.foldl (fun widths port =>
      if port.direction == .input then widths.insert port.name port.width else widths) <|
    module.registers.foldl (fun widths register =>
      widths.insert register.name register.width) ({} : Std.HashMap String Nat)
  let memWidths := module.memories.foldl (fun widths memory =>
    widths.insert memory.name memory.dataWidth) ({} : Std.HashMap String Nat)
  module.expressionRoots.all (·.readsDeclaredB regWidths memWidths)

/-- Pointer-memoized executable implementation of the exact source-level
read check. Unlike checking the expanded dependent `Hw.Expr` graph after
lowering, this visits the compact neutral DAG before bit-plane write actions
and width-normalization nodes are constructed. -/
private unsafe def sourceReadsDeclaredImpl (module : Module) : Bool := Id.run do
  let regWidths := module.ports.foldl (fun widths port =>
      if port.direction == .input then widths.insert port.name port.width else widths) <|
    module.registers.foldl (fun widths register =>
      widths.insert register.name register.width) ({} : Std.HashMap String Nat)
  let memWidths := module.memories.foldl (fun widths memory =>
    widths.insert memory.name memory.dataWidth) ({} : Std.HashMap String Nat)
  let bucketCount := 65536
  let mut seen : Array (List USize) := Array.replicate bucketCount []
  let mut work := module.expressionRoots
  while !work.isEmpty do
    let some expression := work.head? | return false
    work := work.tail
    let key := ptrAddrUnsafe expression
    let bucketIndex := (key.toNat >>> 4) &&& (bucketCount - 1)
    let bucket := seen[bucketIndex]!
    unless bucket.contains key do
      seen := seen.set! bucketIndex (key :: bucket)
      match expression with
      | .literal .. | .partialLiteral .. => pure ()
      | .signal width name _ =>
          unless regWidths.get? name == some width do return false
      | .unary _ _ value _ | .slice _ value _ _ |
          .zeroExtend _ value _ | .signExtend _ value _ =>
          work := value :: work
      | .binary _ _ left right _ | .concat _ left right _ =>
          work := left :: right :: work
      | .mux _ condition yes no _ =>
          work := condition :: yes :: no :: work
      | .memoryRead width memory address _ =>
          unless memWidths.get? memory == some width do return false
          work := address :: work
  return true

attribute [implemented_by sourceReadsDeclaredImpl] Module.sourceReadsDeclaredB

private def failAt {m : Type → Type} {α : Type} [Monad m] [MonadExcept String m]
    (source : SourceLocation) (message : String) : m α :=
  throw s!"{source.render}: {message}"

private def expectWidth {m : Type → Type} [Monad m] [MonadExcept String m]
    (expected : Nat) (expression : LoweredExpr)
    (source : SourceLocation) : m (Loom.Hw.Expr expected) := do
  if equal : expression.width = expected then
    return equal ▸ expression.value
  else
    failAt source s!"expression width {expression.width} does not match expected width {expected}"

def lowerExpr? : Expr → Except String LoweredExpr
  | .literal width value source => do
      if width == 0 then failAt source "zero-width literal"
      return ⟨width, .lit (BitVec.ofNat width value)⟩
  | .partialLiteral width choice source => do
      unless choice.validB width do
        failAt source s!"partial value site '{choice.site}' has an invalid or non-refining implementation choice"
      return ⟨width, .lit (BitVec.ofNat width choice.implementationValue)⟩
  | .signal width name source => do
      if width == 0 || name.isEmpty then failAt source "invalid signal reference"
      return ⟨width, .reg width name⟩
  | .unary width op value source => do
      let lowered ← lowerExpr? value
      match op with
      | .bitNot | .negate =>
          let value ← expectWidth width lowered source
          match op with
          | .bitNot => return ⟨width, .not value⟩
          | .negate => return ⟨width, .sub (.lit (BitVec.ofNat width 0)) value⟩
          | _ => failAt source "internal width-preserving unary lowering error"
      | .reduceBool | .reduceAnd | .logicalNot =>
          if width != 1 then failAt source "logical/reduction result width must be one"
          else
            let zero : Loom.Hw.Expr lowered.width := .lit 0
            let allOnes : Loom.Hw.Expr lowered.width := .lit (BitVec.allOnes lowered.width)
            match op with
            | .reduceBool => return ⟨1, .not (.eq lowered.value zero)⟩
            | .reduceAnd => return ⟨1, .eq lowered.value allOnes⟩
            | .logicalNot => return ⟨1, .eq lowered.value zero⟩
            | _ => failAt source "internal reducing unary lowering error"
  | .binary width op left right source => do
      let left ← lowerExpr? left
      let right ← lowerExpr? right
      match op with
      | .equal | .unsignedLessThan | .signedLessThan =>
          if width != 1 then failAt source "comparison result width must be one"
          else
            let leftValue ← expectWidth left.width left source
            let rightValue ← expectWidth left.width right source
            match op with
            | .equal => return ⟨1, .eq leftValue rightValue⟩
            | .unsignedLessThan => return ⟨1, .ult leftValue rightValue⟩
            | .signedLessThan => return ⟨1, .slt leftValue rightValue⟩
            | _ => failAt source "internal comparison lowering error"
      | _ =>
          let left ← expectWidth width left source
          let right ← expectWidth width right source
          let value : Loom.Hw.Expr width := match op with
            | .bitAnd => .and left right
            | .bitOr => .or left right
            | .bitXor => .xor left right
            | .add => .add left right
            | .sub => .sub left right
            | .mul => .mul left right
            | .unsignedDiv => .udiv left right
            | .unsignedRem => .urem left right
            | .shiftLeft => .shl left right
            | .logicalShiftRight => .shr left right
            | _ => .lit 0
          return ⟨width, value⟩
  | .mux width condition yes no source => do
      let condition ← lowerExpr? condition >>= fun value => expectWidth 1 value source
      let yes ← lowerExpr? yes >>= fun value => expectWidth width value source
      let no ← lowerExpr? no >>= fun value => expectWidth width value source
      return ⟨width, .mux condition yes no⟩
  | .slice width value offset source => do
      let value ← lowerExpr? value
      if width == 0 || offset + width > value.width then
        failAt source "slice is empty or outside its operand"
      else return ⟨width, .slice value.value offset width⟩
  | .zeroExtend width value source => do
      let value ← lowerExpr? value
      if width < value.width then failAt source "zero extension narrows its operand"
      else return ⟨width, .zext value.value width⟩
  | .signExtend width value source => do
      let value ← lowerExpr? value
      if width < value.width then failAt source "sign extension narrows its operand"
      else return ⟨width, .sext value.value width⟩
  | .concat width high low source => do
      let high ← lowerExpr? high
      let low ← lowerExpr? low
      if equal : high.width + low.width = width then
        return ⟨width, equal ▸ Loom.Hw.Expr.concat high.value low.value⟩
      else failAt source "concatenation result width is inconsistent"
  | .memoryRead width memory address source => do
      let address ← lowerExpr? address
      if width == 0 || memory.isEmpty then failAt source "invalid memory read"
      return ⟨width, .memRead width memory address.value⟩

/-! The schema-v2 parser preserves physical sharing from its postorder
expression table.  The transparent reference lowering above is intentionally
ordinary structural recursion, but treating that DAG as a tree can repeat a
large cache mux cone exponentially.  This executable twin memoizes only
pointer-equal source nodes. Pointer equality implies value equality, and the
cached result is produced by the same cases and checks as `lowerExpr?`.
The kernel-visible definition remains the transparent reference function. -/

private unsafe def lowerExprMemoGo
    (expression : Expr) :
    StateT (Std.HashMap USize LoweredExpr) (Except String) LoweredExpr := do
  let key := ptrAddrUnsafe expression
  if let some lowered := (← get).get? key then
    return lowered
  let lowered ← match expression with
    | .literal width value source => do
        if width == 0 then failAt source "zero-width literal"
        return ⟨width, .lit (BitVec.ofNat width value)⟩
    | .partialLiteral width choice source => do
        unless choice.validB width do
          failAt source
            s!"partial value site '{choice.site}' has an invalid or non-refining implementation choice"
        return ⟨width, .lit (BitVec.ofNat width choice.implementationValue)⟩
    | .signal width name source => do
        if width == 0 || name.isEmpty then failAt source "invalid signal reference"
        return ⟨width, .reg width name⟩
    | .unary width op value source => do
        let value ← lowerExprMemoGo value
        match op with
        | .bitNot | .negate =>
            let value ← expectWidth width value source
            match op with
            | .bitNot => return ⟨width, .not value⟩
            | .negate =>
                return ⟨width, .sub (.lit (BitVec.ofNat width 0)) value⟩
            | _ => failAt source "internal width-preserving unary lowering error"
        | .reduceBool | .reduceAnd | .logicalNot =>
            if width != 1 then
              failAt source "logical/reduction result width must be one"
            else
              let zero : Loom.Hw.Expr value.width := .lit 0
              let allOnes : Loom.Hw.Expr value.width :=
                .lit (BitVec.allOnes value.width)
              match op with
              | .reduceBool => return ⟨1, .not (.eq value.value zero)⟩
              | .reduceAnd => return ⟨1, .eq value.value allOnes⟩
              | .logicalNot => return ⟨1, .eq value.value zero⟩
              | _ => failAt source "internal reducing unary lowering error"
    | .binary width op left right source => do
        let left ← lowerExprMemoGo left
        let right ← lowerExprMemoGo right
        match op with
        | .equal | .unsignedLessThan | .signedLessThan =>
            if width != 1 then
              failAt source "comparison result width must be one"
            else
              let leftValue ← expectWidth left.width left source
              let rightValue ← expectWidth left.width right source
              match op with
              | .equal => return ⟨1, .eq leftValue rightValue⟩
              | .unsignedLessThan => return ⟨1, .ult leftValue rightValue⟩
              | .signedLessThan => return ⟨1, .slt leftValue rightValue⟩
              | _ => failAt source "internal comparison lowering error"
        | _ =>
            let left ← expectWidth width left source
            let right ← expectWidth width right source
            let value : Loom.Hw.Expr width := match op with
              | .bitAnd => .and left right
              | .bitOr => .or left right
              | .bitXor => .xor left right
              | .add => .add left right
              | .sub => .sub left right
              | .mul => .mul left right
              | .unsignedDiv => .udiv left right
              | .unsignedRem => .urem left right
              | .shiftLeft => .shl left right
              | .logicalShiftRight => .shr left right
              | _ => .lit 0
            return ⟨width, value⟩
    | .mux width condition yes no source => do
        let condition ← lowerExprMemoGo condition >>=
          fun value => expectWidth 1 value source
        let yes ← lowerExprMemoGo yes >>= fun value => expectWidth width value source
        let no ← lowerExprMemoGo no >>= fun value => expectWidth width value source
        return ⟨width, .mux condition yes no⟩
    | .slice width value offset source => do
        let value ← lowerExprMemoGo value
        if width == 0 || offset + width > value.width then
          failAt source "slice is empty or outside its operand"
        else return ⟨width, .slice value.value offset width⟩
    | .zeroExtend width value source => do
        let value ← lowerExprMemoGo value
        if width < value.width then failAt source "zero extension narrows its operand"
        else return ⟨width, .zext value.value width⟩
    | .signExtend width value source => do
        let value ← lowerExprMemoGo value
        if width < value.width then failAt source "sign extension narrows its operand"
        else return ⟨width, .sext value.value width⟩
    | .concat width high low source => do
        let high ← lowerExprMemoGo high
        let low ← lowerExprMemoGo low
        if equal : high.width + low.width = width then
          return ⟨width, equal ▸ Loom.Hw.Expr.concat high.value low.value⟩
        else failAt source "concatenation result width is inconsistent"
    | .memoryRead width memory address source => do
        let address ← lowerExprMemoGo address
        if width == 0 || memory.isEmpty then failAt source "invalid memory read"
        return ⟨width, .memRead width memory address.value⟩
  modify (·.insert key lowered)
  return lowered

private unsafe def lowerExprImpl (expression : Expr) : Except String LoweredExpr :=
  (lowerExprMemoGo expression).run' {}

attribute [implemented_by lowerExprImpl] lowerExpr?

def lowerExprs? (expressions : List Expr) :
    Except String (List LoweredExpr) :=
  expressions.mapM lowerExpr?

/-- The batch implementation deliberately shares one source-node cache across
all roots in a module. Cache write enables, read addresses, next-state roots,
and outputs frequently point into the same Yosys expression DAG; restarting a
cache at each root recreates that DAG many times even when each root is itself
linear-time. -/
private unsafe def lowerExprsImpl (expressions : List Expr) :
    Except String (List LoweredExpr) := do
  -- An ordinary recursive StateT walk still retains one dependent monadic
  -- continuation per source-DAG level.  Large generated mux/memory cones can
  -- therefore consume far more heap than their node count suggests even when
  -- every node result is memoized.  Visit the same cases in explicit
  -- postorder: once a node's children are cached, `lowerExprMemoGo` performs
  -- exactly one shallow lowering step and returns immediately on each child.
  let children : Expr → List Expr
    | .literal .. | .partialLiteral .. | .signal .. => []
    | .unary _ _ value _ | .slice _ value _ _ |
        .zeroExtend _ value _ | .signExtend _ value _ => [value]
    | .binary _ _ left right _ | .concat _ left right _ => [left, right]
    | .mux _ condition yes no _ => [condition, yes, no]
    | .memoryRead _ _ address _ => [address]
  let mut cache : Std.HashMap USize LoweredExpr := {}
  let mut work := expressions.map (·, false)
  while !work.isEmpty do
    let some (expression, expanded) := work.head?
      | throw "internal expression worklist mismatch"
    work := work.tail
    let key := ptrAddrUnsafe expression
    unless cache.contains key do
      if expanded then
        let (lowered, nextCache) ← (lowerExprMemoGo expression).run cache
        cache := nextCache.insert key lowered
      else
        work := (children expression).map (·, false) ++
          (expression, true) :: work
  expressions.mapM fun expression =>
    match cache.get? (ptrAddrUnsafe expression) with
    | some lowered => pure lowered
    | none => throw "internal lowered-expression cache mismatch"

attribute [implemented_by lowerExprsImpl] lowerExprs?

private def sequence (actions : List Act) : Act :=
  actions.foldl .seq .skip

private def inputPort (domain : ClockDomain) (port : Port) : Bool :=
  port.direction == .input && port.name != domain.clockPort &&
    domain.reset.port.all (· != port.name)

private def checkModuleBoundary (module : Module) : Except String ClockDomain := do
  unless module.source.validB do failAt module.source "invalid module source location"
  if module.name.isEmpty then failAt module.source "module name is empty"
  match module.unsupported with
  | first :: _ =>
      failAt first.source s!"unsupported imported construct '{first.kind}': {first.detail}"
  | [] => pure ()
  unless module.domains.length == 1 do
    failAt module.source s!"ordinary Design lowering requires exactly one clock domain, found {module.domains.length}"
  let some domain := module.domains[0]?
    | failAt module.source "clock domain disappeared after exact-count check"
  if domain.name.isEmpty || domain.clockPort.isEmpty then
    failAt domain.source "clock domain name/port is empty"
  unless module.ports.any (fun port =>
      port.name == domain.clockPort && port.direction == .input && port.width == 1) do
    failAt domain.source s!"clock port '{domain.clockPort}' is absent or not a one-bit input"
  match domain.reset.kind with
  | .asynchronous | .asynchronousAssertSynchronousRelease =>
      failAt domain.source "asynchronous-reset state must remain behind an ExternalComponent contract"
  | .synchronous =>
      let some resetPort := domain.reset.port
        | failAt domain.source "synchronous reset has no port"
      unless module.ports.any (fun port =>
          port.name == resetPort && port.direction == .input && port.width == 1) do
        failAt domain.source s!"reset port '{resetPort}' is absent or not a one-bit input"
      if resetPort == domain.clockPort then
        failAt domain.source "clock and reset ports must be distinct"
  | .resetless =>
      if domain.reset.port.isSome then
        failAt domain.source "resetless clock domain must not name a reset port"
  if module.ports.any (·.direction == .inout) then
    failAt module.source "inout ports require an explicit external pad/tri-state contract"
  if !Inventory.uniqueB (module.ports.map (·.name)) then
    failAt module.source "duplicate port names"
  if !Inventory.uniqueB (module.registers.map (·.name)) then
    failAt module.source "duplicate register names"
  unless module.registers.all (fun register =>
      register.domain.all (· == domain.name)) do
    failAt module.source "register names a clock domain other than the lowered domain"
  if !Inventory.uniqueB (module.memories.map (·.name)) then
    failAt module.source "duplicate memory names"
  if !Inventory.uniqueB (module.outputs.map (·.name)) then
    failAt module.source "duplicate output drivers"
  return domain

structure LoweredModule where
  design : Design
  edge : Loom.ClockEdge
  clockPort : String
  reset : Reset
  source : SourceLocation

structure LoweredStatelessModule where
  implementation : StatelessDesign
  source : SourceLocation

inductive LoweredAnyModule where
  | clocked (module : LoweredModule)
  | stateless (module : LoweredStatelessModule)

/-- Checked lowering of module-owned logic. Child instances remain in the IR
for hierarchy-preserving assembly and do not get flattened into this Design. -/
def Module.lowerLocalDesign? (module : Module) : Except String LoweredModule := do
  unless module.instances.isEmpty do
    failAt module.source
      "hierarchical imports require checked package lowering; single-module lowering cannot bind child nets"
  let domain ← checkModuleBoundary module
  unless module.sourceReadsDeclaredB do
    failAt module.source
      "source expression reads an undeclared or wrong-width register/input/memory"
  let roots := module.expressionRoots
  let loweredRoots ← lowerExprs? roots
  let mut rootIndex := 0
  let mut regs : List RegDecl := []
  let mut actions : List Act := []
  for register in module.registers do
    if register.width == 0 || register.name.isEmpty then
      failAt register.source "invalid register declaration"
    let some loweredNext := loweredRoots[rootIndex]?
      | failAt register.source "internal expression-root inventory mismatch"
    rootIndex := rootIndex + 1
    let next ← expectWidth register.width loweredNext register.source
    regs := regs ++ [⟨register.name, register.width,
      BitVec.ofNat register.width register.init⟩]
    actions := actions ++ [.write register.width register.name next]
  let mut memories : List MemDecl := []
  for memory in module.memories do
    if memory.name.isEmpty || memory.addressWidth == 0 || memory.dataWidth == 0 then
      failAt memory.source "invalid memory declaration"
    let cells := 2 ^ memory.addressWidth
    unless memory.init.length == cells do
      failAt memory.source
        s!"memory initialization has {memory.init.length} cells, expected {cells}"
    unless memory.init.all (· < 2 ^ memory.dataWidth) do
      failAt memory.source "memory initialization value exceeds its data width"
    let packedInit := memory.init.foldr
      (fun value rest => value + 2 ^ memory.dataWidth * rest) 0
    match memory.initRefinement with
    | none => pure ()
    | some choice =>
        let packedWidth := cells * memory.dataWidth
        unless choice.validB packedWidth do
          failAt memory.source "invalid partial memory-initialization refinement"
        unless choice.implementationValue == packedInit do
          failAt memory.source
            "partial memory-initialization refinement disagrees with concrete image"
    memories := memories ++
      [{ name := memory.name, addrWidth := memory.addressWidth,
         dataWidth := memory.dataWidth,
         init := fun address => BitVec.ofNat memory.dataWidth (memory.init.getD address 0) }]
    for write in memory.writes do
      let some loweredEnable := loweredRoots[rootIndex]?
        | failAt write.source "internal expression-root inventory mismatch"
      rootIndex := rootIndex + 1
      let some loweredAddress := loweredRoots[rootIndex]?
        | failAt write.source "internal expression-root inventory mismatch"
      rootIndex := rootIndex + 1
      let some loweredData := loweredRoots[rootIndex]?
        | failAt write.source "internal expression-root inventory mismatch"
      rootIndex := rootIndex + 1
      let enable ← expectWidth 1 loweredEnable write.source
      let address ← expectWidth memory.addressWidth loweredAddress write.source
      let data ← expectWidth memory.dataWidth loweredData write.source
      actions := actions ++ [.ite enable
        (.memWrite memory.addressWidth memory.dataWidth memory.name write.port address data)
        .skip]
  let mut outputs : List CombOutput := []
  for output in module.outputs do
    let some loweredValue := loweredRoots[rootIndex]?
      | failAt output.source "internal expression-root inventory mismatch"
    rootIndex := rootIndex + 1
    let value ← expectWidth output.width loweredValue output.source
    outputs := outputs ++ [⟨output.name, output.width, value⟩]
  unless rootIndex == loweredRoots.length do
    failAt module.source "internal expression-root inventory has unused entries"
  let design : Design :=
    { name := module.name
      regs := regs
      mems := memories
      rules := [⟨"imported_next_state", sequence actions⟩]
      inputs := (module.ports.filter (inputPort domain)).map fun port =>
        ⟨port.name, port.width⟩
      outputs := []
      combOutputs := outputs }
  design.emitCheckAfterImportReads
  return ⟨design, domain.edge, domain.clockPort, domain.reset, module.source⟩

private def checkStatelessBoundary (module : Module) : Except String Unit := do
  unless module.source.validB do failAt module.source "invalid module source location"
  if module.name.isEmpty then failAt module.source "module name is empty"
  match module.unsupported with
  | first :: _ =>
      failAt first.source s!"unsupported imported construct '{first.kind}': {first.detail}"
  | [] => pure ()
  unless module.domains.isEmpty do
    failAt module.source "stateless lowering requires no clock domains"
  unless module.registers.isEmpty && module.memories.isEmpty do
    failAt module.source "stateless lowering cannot contain registers or memories"
  if module.ports.any (·.direction == .inout) then
    failAt module.source "inout ports require an explicit external pad/tri-state contract"
  unless module.ports.all (fun port =>
      port.source.validB && !port.name.isEmpty && port.width > 0) do
    failAt module.source "invalid stateless port declaration"
  if !Inventory.uniqueB (module.ports.map (·.name)) then
    failAt module.source "duplicate port names"
  if !Inventory.uniqueB (module.outputs.map (·.name)) then
    failAt module.source "duplicate output drivers"
  unless module.outputs.all (fun output =>
      module.ports.any fun port =>
        port.name == output.name && port.direction == .output &&
          port.width == output.width) do
    failAt module.source "an output driver does not match an output port"
  unless (module.ports.filter (·.direction == .output)).all (fun port =>
      module.outputs.any fun output =>
        output.name == port.name && output.width == port.width) do
    failAt module.source "an output port has no exact driver"

/-- Checked lowering for a module with no sequential state.  It produces an
ordinary Design wrapped by a proof that its behavior is only the pure
input-to-output relation. -/
def Module.lowerStatelessDesign? (module : Module) :
    Except String LoweredStatelessModule := do
  unless module.instances.isEmpty do
    failAt module.source
      "hierarchical imports require checked package lowering; single-module lowering cannot bind child nets"
  checkStatelessBoundary module
  let mut outputs : List CombOutput := []
  for output in module.outputs do
    let value ← lowerExpr? output.value >>= fun value =>
      expectWidth output.width value output.source
    outputs := outputs ++ [⟨output.name, output.width, value⟩]
  let design : Design :=
    { name := module.name
      regs := []
      mems := []
      rules := []
      inputs := (module.ports.filter (·.direction == .input)).map fun port =>
        ⟨port.name, port.width⟩
      outputs := []
      combOutputs := outputs }
  return ⟨← StatelessDesign.check? design, module.source⟩

/-- Select the checked module kind from the explicit domain inventory. -/
def Module.lowerAny? (module : Module) : Except String LoweredAnyModule :=
  if module.domains.isEmpty then
    return .stateless (← module.lowerStatelessDesign?)
  else
    return .clocked (← module.lowerLocalDesign?)

/-- Domain-polymorphic component template for a checked stateless import. -/
def Module.lowerStatelessComponent? (module : Module) :
    Except String StatelessComponent := do
  let lowered ← module.lowerStatelessDesign?
  let component : StatelessComponent :=
    { name := module.name
      ports := module.ports.map (fun port =>
        ⟨port.name,
          match port.direction with
          | .input => Loom.Hw.PortDirection.input
          | .output => Loom.Hw.PortDirection.output
          | .inout => Loom.Hw.PortDirection.input,
          port.width, port.semanticType⟩)
      implementation := lowered.implementation }
  return component

/-- Build the erased-but-checked component boundary used by dynamic importers.
Nominally typed authored designs should continue to use `DomainComponent`. -/
def Module.lowerComponent? (module : Module) : Except String Component.Sealed := do
  let lowered ← module.lowerLocalDesign?
  let some domain := module.domains[0]?
    | failAt module.source "clock domain disappeared after checked lowering"
  let interface : ComponentInterface :=
    ⟨module.ports.filterMap fun port =>
      if port.name == domain.clockPort || domain.reset.port.any (· == port.name) then none
      else match port.direction with
        | .input => some ⟨port.name, .input, port.width, port.semanticType, domain.name⟩
        | .output => some ⟨port.name, .output, port.width, port.semanticType, domain.name⟩
        | .inout => none⟩
  Component.seal?
    { name := module.name, interface := interface, design := lowered.design }

end Loom.Hw.ImportIR
