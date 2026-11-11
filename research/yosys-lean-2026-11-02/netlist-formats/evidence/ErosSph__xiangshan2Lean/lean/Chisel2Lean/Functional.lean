import Lean

/-! Pure interpreter for the typed, fixed-configuration FunctionalIR.
    The source emitter remains available. This representation avoids one Lean
    declaration for every field of a very large circuit. -/
namespace Chisel2Lean.Functional

inductive HSort where
  | bits (width : Nat)
  | boolean
  | memory (addressWidth width : Nat)
  deriving BEq, Repr

inductive Value where
  | bits (width : Nat) (value : BitVec width)
  | boolean (value : Bool)
  | memory (addressWidth width : Nat) (words : Array (BitVec width))
  deriving Repr

def arrayAt (values : Array α) (index : Nat) : Except String α :=
  match values[index]? with
  | some value => .ok value
  | none => .error s!"Array index {index} is out of range ({values.size})"

def require (condition : Bool) (message : String) : Except String Unit :=
  if condition then .ok () else .error message

def Value.sort : Value → HSort
  | .bits width _ => .bits width
  | .boolean _ => .boolean
  | .memory addressWidth width _ => .memory addressWidth width

def Value.conforms (value : Value) (sort : HSort) : Bool :=
  value.sort == sort && match value with
    | .memory addressWidth _ words => words.size == 2 ^ addressWidth
    | _ => true

def Value.asBits (value : Value) (width : Nat) : Except String (BitVec width) :=
  match value with
  | .bits n b =>
    if h : n = width then .ok (h ▸ b) else .error "Bitvector width mismatch"
  | _ => .error "Expected a bitvector"

def Value.asBool : Value → Except String Bool
  | .boolean value => .ok value
  | _ => .error "Expected a Boolean"

def Value.nonzero : Value → Except String Bool
  | .bits _ value => .ok (value != 0)
  | _ => .error "Expected a bitvector predicate"

def Value.toJson : Value → Lean.Json
  | .bits _ value => Lean.toJson value.toNat
  | .boolean value => Lean.toJson value
  | .memory _ _ words => Lean.toJson (words.map BitVec.toNat)

def boundedShiftLeft (value : BitVec width) (amount : Nat) : BitVec width :=
  value <<< Nat.min width amount

def boundedShiftRight (value : BitVec width) (amount : Nat) : BitVec width :=
  value >>> Nat.min width amount

def boundedArithShiftRight (value : BitVec width) (amount : Nat) : BitVec width :=
  value.sshiftRight (Nat.min width amount)

theorem boundedShiftLeft_eq (value : BitVec width) (amount : Nat) :
    boundedShiftLeft value amount = value <<< amount := by
  unfold boundedShiftLeft
  rw [Nat.min_eq_min]
  by_cases h : width ≤ amount
  · rw [Nat.min_eq_left h, BitVec.shiftLeft_eq_zero (Nat.le_refl width), BitVec.shiftLeft_eq_zero h]
  · rw [Nat.min_eq_right (Nat.le_of_lt (Nat.lt_of_not_ge h))]

theorem boundedShiftRight_eq (value : BitVec width) (amount : Nat) :
    boundedShiftRight value amount = value >>> amount := by
  unfold boundedShiftRight
  rw [Nat.min_eq_min]
  by_cases h : width ≤ amount
  · rw [Nat.min_eq_left h, BitVec.ushiftRight_eq_zero (Nat.le_refl width), BitVec.ushiftRight_eq_zero h]
  · rw [Nat.min_eq_right (Nat.le_of_lt (Nat.lt_of_not_ge h))]

theorem boundedArithShiftRight_eq (value : BitVec width) (amount : Nat) :
    boundedArithShiftRight value amount = value.sshiftRight amount := by
  unfold boundedArithShiftRight
  cases h : value.msb with
  | false =>
    simp only [BitVec.sshiftRight_eq_of_msb_false h]
    exact boundedShiftRight_eq value amount
  | true =>
    simp only [BitVec.sshiftRight_eq_of_msb_true h]
    exact congrArg (fun x => ~~~x) (boundedShiftRight_eq (~~~value) amount)

structure Field where
  name : String
  sort : HSort
  known : Array (Nat × Nat) := #[]
  next : Option Nat := none
  deriving Repr

structure Node where
  operation : Nat
  sort : HSort
  args : Array Nat
  extra : Nat := 0
  line : Nat := 0
  deriving Repr

structure Output where
  name : String
  node : Nat
  guard : Option Nat := none
  observation : Bool := false
  deriving Repr

structure Effect where
  kind : Nat -- assert, assume, cover, print
  -- These indices refer to output observation ports, not internal node IDs.
  enable : Nat
  condition : Option Nat
  arguments : Option Nat
  source : String
  format : String
  fileDescriptor : Option Nat := none
  stream : String := ""
  priorityBits : String := ""
  deriving Repr

structure Circuit where
  inputs : Array Field
  states : Array Field
  outputs : Array Output
  nodes : Array Node
  effects : Array Effect := #[]
  clock : Option Nat := none
  deriving Repr

def validateNode (inputs states : Array Field) (previous : Array Node) (node : Node) : Except String Unit := do
  let sorts ← node.args.mapM fun index => return (← arrayAt previous index).sort
  let arity (n : Nat) := require (sorts.size == n) "Operation arity mismatch"
  let bits (index : Nat) : Except String Nat := do
    match ← arrayAt sorts index with
    | .bits width => return width
    | _ => throw "Expected a bitvector argument"
  let sameBits (n : Nat) : Except String Nat := do
    arity n
    let width ← bits 0
    for sort in sorts do require (sort == .bits width) "Operation operand width mismatch"
    return width
  match node.operation with
  | 0 =>
    arity 0
    match node.sort with
    | .boolean => require (node.extra < 2) "Malformed Boolean constant"
    | .bits width => require (width > 0) "Zero-width constant"
    | _ => throw "Memory constants are not in the serializer contract"
  | 1 | 2 =>
    arity 0
    let field ← arrayAt (if node.operation == 1 then inputs else states) node.extra
    require (field.sort == node.sort) "Field reference sort mismatch"
  | 3 =>
    arity 3
    require ((← arrayAt sorts 0) == .boolean && (← arrayAt sorts 1) == node.sort &&
             (← arrayAt sorts 2) == node.sort) "Conditional sort mismatch"
  | 4 =>
    arity 2
    require (node.sort == .bits ((← bits 0) + (← bits 1))) "Concatenation width mismatch"
  | 5 =>
    arity 1
    match node.sort with
    | .bits width => require (width > 0 && node.extra + width ≤ (← bits 0)) "Extract bounds mismatch"
    | _ => throw "Extract must produce bits"
  | 6 | 7 =>
    arity 1
    match node.sort with
    | .bits width => require (width ≥ (← bits 0)) "Extension narrows its argument"
    | _ => throw "Extension must produce bits"
  | 8 | 9 => require (node.sort == .bits (← sameBits 1)) "Unary result sort mismatch"
  | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 =>
    require (node.sort == .bits (← sameBits 2)) "Binary result sort mismatch"
  | 20 =>
    require (sorts.size > 0) "Empty XOR"
    let width ← bits 0
    for sort in sorts do require (sort == .bits width) "XOR operand sort mismatch"
    require (node.sort == .bits width) "XOR result sort mismatch"
  | 21 | 22 | 23 | 24 | 25 | 26 =>
    let _ ← sameBits 2
    require (node.sort == .boolean) "Comparison must produce a Boolean"
  | 27 | 28 =>
    arity (if node.operation == 27 then 2 else 3)
    match ← arrayAt sorts 0 with
    | .memory addressWidth width =>
      require ((← arrayAt sorts 1) == .bits addressWidth) "Memory address width mismatch"
      if node.operation == 27 then
        require (node.sort == .bits width) "Memory read width mismatch"
      else
        require ((← arrayAt sorts 2) == .bits width && node.sort == .memory addressWidth width)
          "Memory write sort mismatch"
    | _ => throw "Expected a memory argument"
  | _ => throw s!"Unsupported FunctionalIR opcode {node.operation} at SMT line {node.line}"

def evaluateNode (inputs states : Array Value) (values : Array Value) (domains : Array Bool)
    (node : Node) : Except String (Value × Bool) := do
  if node.operation == 0 then
    match node.sort with
    | .bits width => return (.bits width (BitVec.ofNat width node.extra), true)
    | .boolean => return (.boolean (node.extra != 0), true)
    | _ => throw "Invalid constant sort"
  if node.operation == 1 || node.operation == 2 then
    return (← arrayAt (if node.operation == 1 then inputs else states) node.extra, true)
  let args ← node.args.mapM (arrayAt values)
  let deps ← node.args.mapM (arrayAt domains)
  let defined := deps.all id
  if node.operation == 3 then
    let condition ← (← arrayAt args 0).asBool
    let index := if condition then 1 else 2
    return (← arrayAt args index, (← arrayAt deps 0) && (← arrayAt deps index))
  if node.operation == 27 || node.operation == 28 then
    match ← arrayAt args 0 with
    | .memory addressWidth width words =>
      let address ← (← arrayAt args 1).asBits addressWidth
      let index := address.toNat
      if h : index < words.size then
        if node.operation == 27 then return (.bits width words[index], defined)
        let word ← (← arrayAt args 2).asBits width
        return (.memory addressWidth width (words.set index word), defined)
      else throw "Memory value has an invalid word count"
    | _ => throw "Expected a memory value"
  match ← arrayAt args 0 with
  | .bits width a =>
    match node.operation with
    | 4 =>
      match ← arrayAt args 1 with
      | .bits rightWidth b => return (.bits (width + rightWidth) (a ++ b), defined)
      | _ => throw "Expected concatenation bits"
    | 5 | 6 | 7 =>
      match node.sort with
      | .bits resultWidth =>
        let result := if node.operation == 5 then BitVec.extractLsb' node.extra resultWidth a
          else if node.operation == 6 then a.setWidth resultWidth else a.signExtend resultWidth
        return (.bits resultWidth result, defined)
      | _ => throw "Expected bitvector result sort"
    | 8 => return (.bits width (~~~a), defined)
    | 9 => return (.bits width (-a), defined)
    | 20 =>
      let mut result := a
      for index in [1:args.size] do result := result ^^^ (← (← arrayAt args index).asBits width)
      return (.bits width result, defined)
    | operation =>
      let b ← (← arrayAt args 1).asBits width
      match operation with
      | 10 => return (.bits width (BitVec.udiv a b), defined && b != 0)
      | 11 => return (.bits width (BitVec.umod a b), defined && b != 0)
      | 12 => return (.bits width (boundedShiftLeft a b.toNat), defined)
      | 13 => return (.bits width (boundedShiftRight a b.toNat), defined)
      | 14 => return (.bits width (boundedArithShiftRight a b.toNat), defined)
      | 15 => return (.bits width (a + b), defined)
      | 16 => return (.bits width (a - b), defined)
      | 17 => return (.bits width (a * b), defined)
      | 18 => return (.bits width (a &&& b), defined)
      | 19 => return (.bits width (a ||| b), defined)
      | 21 => return (.boolean (a == b), defined)
      | 22 => return (.boolean (a != b), defined)
      | 23 => return (.boolean (decide (a.toNat > b.toNat)), defined)
      | 24 => return (.boolean (decide (a.toNat ≥ b.toNat)), defined)
      | 25 => return (.boolean (decide (a.toInt > b.toInt)), defined)
      | 26 => return (.boolean (decide (a.toInt ≥ b.toInt)), defined)
      | _ => throw "Unsupported operation during evaluation"
  | _ => throw "Expected a bitvector operand"

structure Evaluation where
  outputs : Array Value
  nextState : Array Value
  outputDefined : Bool
  transitionDefined : Bool
  checksValid : Bool
  maxLeftShift : Nat := 0
  maxLeftShiftLine : Nat := 0

def validateFrame (fields : Array Field) (values : Array Value) : Except String Unit := do
  require (fields.size == values.size) "Frame field count mismatch"
  for index in [:fields.size] do
    let field ← arrayAt fields index
    require ((← arrayAt values index).conforms field.sort) ("Value sort mismatch at " ++ field.name)

def evaluate (circuit : Circuit) (inputs state : Array Value) : Except String Evaluation := do
  validateFrame circuit.inputs inputs
  validateFrame circuit.states state
  let mut values : Array Value := #[]
  let mut domains : Array Bool := #[]
  let mut maxLeftShift := 0
  let mut maxLeftShiftLine := 0
  for node in circuit.nodes do
    if node.operation == 12 then
      match ← arrayAt values (← arrayAt node.args 1) with
      | .bits _ amount =>
        if amount.toNat > maxLeftShift then
          maxLeftShift := amount.toNat
          maxLeftShiftLine := node.line
      | _ => throw "Shift count must be a bitvector"
    let (value, defined) ← evaluateNode inputs state values domains node
    values := values.push value
    domains := domains.push defined
  let mut outputs := #[]
  let mut nextState := #[]
  let mut outputDefined := true
  let mut transitionDefined := true
  let mut checksValid := true
  for output in circuit.outputs do
    outputs := outputs.push (← arrayAt values output.node)
    let active ← match output.guard with
      | none => pure true
      | some enable => (← arrayAt values enable).nonzero
    outputDefined := outputDefined && (!active || (← arrayAt domains output.node))
  for field in circuit.states do
    let some index := field.next | throw "Missing state transition"
    nextState := nextState.push (← arrayAt values index)
    transitionDefined := transitionDefined && (← arrayAt domains index)
  for effect in circuit.effects do
    if effect.kind < 2 && (← (← arrayAt outputs effect.enable).nonzero) then
      let some index := effect.condition | throw "Missing property condition"
      checksValid := checksValid && (← (← arrayAt outputs index).nonzero)
  return { outputs, nextState, outputDefined, transitionDefined, checksValid, maxLeftShift, maxLeftShiftLine }

def initialValid (fields : Array Field) (state : Array Value) : Except String Bool := do
  validateFrame fields state
  for index in [:fields.size] do
    let field ← arrayAt fields index
    let value ← arrayAt state index
    let check {width : Nat} (word : BitVec width) (known : Nat × Nat) :=
      (word &&& BitVec.ofNat width known.1) == BitVec.ofNat width known.2
    match value with
    | .bits _ bits =>
      require (field.known.size == 1) "Malformed known-bit constraint"
      if !check bits (← arrayAt field.known 0) then return false
    | .memory _ _ words =>
      require (field.known.size == words.size) "Malformed memory constraints"
      for address in [:words.size] do
        if !check (← arrayAt words address) (← arrayAt field.known address) then return false
    | .boolean _ => throw "Boolean state is outside the serializer contract"
  return true

def validateCircuit (circuit : Circuit) : Except String Unit := do
  let validateFields (fields : Array Field) (state : Bool) : Except String Unit := do
    let mut names : Std.HashSet String := {}
    for field in fields do
      require (!names.contains field.name) ("Duplicate field " ++ field.name)
      names := names.insert field.name
      match field.sort with
      | .bits width =>
        require (width > 0) "Zero-width field"
        if state then require (field.known.size == 1) "Malformed state known bits"
      | .memory addressWidth width =>
        require (state && width > 0 && addressWidth > 0 && field.known.size == 2 ^ addressWidth)
          "Malformed memory state"
      | .boolean => throw "Hardware fields must be bitvectors or memory states"
  validateFields circuit.inputs false
  validateFields circuit.states true
  let mut previous : Array Node := #[]
  for node in circuit.nodes do
    validateNode circuit.inputs circuit.states previous node
    previous := previous.push node
  for field in circuit.states do
    let some index := field.next | throw "Missing next-state reference"
    require ((← arrayAt circuit.nodes index).sort == field.sort) "Next-state sort mismatch"
  let predicate (index : Nat) : Except String Unit := do
    require ((← arrayAt circuit.nodes index).sort == .bits 1) "Predicate must be one bit"
  let mut outputNames : Std.HashSet String := {}
  for output in circuit.outputs do
    require (!outputNames.contains output.name) "Duplicate output name"
    outputNames := outputNames.insert output.name
    match (← arrayAt circuit.nodes output.node).sort with
    | .bits width => require (width > 0) "Zero-width output"
    | _ => throw "Hardware outputs must be bitvectors"
    if let some index := output.guard then predicate index
  for effect in circuit.effects do
    require (effect.kind < 4) "Unknown observation kind"
    predicate (← arrayAt circuit.outputs effect.enable).node
    if effect.kind < 3 then
      let some index := effect.condition | throw "Missing property condition"
      predicate (← arrayAt circuit.outputs index).node
    if let some index := effect.arguments then
      match (← arrayAt circuit.nodes (← arrayAt circuit.outputs index).node).sort with
      | .bits width => require (width > 0) "Empty observation argument"
      | _ => throw "Observation argument must be a bitvector"
  if let some index := circuit.clock then
    require ((← arrayAt circuit.inputs index).sort == .bits 1) "Clock input must be one bit"

structure CheckedCircuit where
  data : Circuit
  valid : validateCircuit data = .ok ()

def checkCircuit (circuit : Circuit) : Except String CheckedCircuit :=
  match h : validateCircuit circuit with
  | .ok () => .ok ⟨circuit, h⟩
  | .error message => .error message

structure Cursor where
  bytes : ByteArray
  position : Nat := 0

abbrev Decoder (α : Type) := StateT Cursor (Except String) α

def readByte : Decoder Nat := do
  let cursor ← get
  if h : cursor.position < cursor.bytes.size then
    set { cursor with position := cursor.position + 1 }
    return cursor.bytes[cursor.position].toNat
  else throw "Truncated circuit data"

def readNat : Decoder Nat := do
  let cursor ← get
  let mut result := 0
  let mut shift := 0
  for _ in [:cursor.bytes.size - cursor.position] do
    let byte ← readByte
    result := result + (byte % 128) * 2 ^ shift
    if byte < 128 then return result
    shift := shift + 7
  throw "Unterminated circuit integer"

def readString : Decoder String := do
  let size ← readNat
  let cursor ← get
  if cursor.position + size > cursor.bytes.size then throw "Truncated circuit string"
  let bytes := cursor.bytes.extract cursor.position (cursor.position + size)
  set { cursor with position := cursor.position + size }
  match String.fromUTF8? bytes with
  | some value => return value
  | none => throw "Circuit string is not UTF-8"

def readArray (item : Decoder α) : Decoder (Array α) := do
  let count ← readNat
  let mut result := #[]
  for _ in [:count] do result := result.push (← item)
  return result

def readOption : Decoder (Option Nat) := do
  let value ← readNat
  return if value == 0 then none else some (value - 1)

def readSort : Decoder HSort := do
  match ← readNat with
  | 0 => return .bits (← readNat)
  | 1 => return .boolean
  | 2 => return .memory (← readNat) (← readNat)
  | _ => throw "Unknown circuit sort"

def readField (sorts : Array HSort) : Decoder Field := do
  let name ← readString
  let sort ← arrayAt sorts (← readNat)
  let known ← readArray do return (← readNat, ← readNat)
  let next ← readOption
  return { name, sort, known, next }

def readCircuit : Decoder Circuit := do
  require ((← readString) == "chisel2lean-functional-1") "Unsupported circuit data version"
  let sorts ← readArray readSort
  let inputs ← readArray (readField sorts)
  let states ← readArray (readField sorts)
  let outputs ← readArray do
    let name ← readString
    let node ← readNat
    let guard ← readOption
    let observation ← readNat
    require (observation < 2) "Malformed observation flag"
    return { name, node, guard, observation := observation != 0 : Output }
  let effects ← readArray do
    let kind ← readNat
    let enable ← readNat
    let condition ← readOption
    let arguments ← readOption
    let source ← readString
    let format ← readString
    let fileDescriptor ← readOption
    let stream ← readString
    let priorityBits ← readString
    return { kind, enable, condition, arguments, source, format, fileDescriptor, stream, priorityBits : Effect }
  let clock ← readOption
  let nodes ← readArray do
    let operation ← readNat
    let sort ← arrayAt sorts (← readNat)
    let args ← readArray readNat
    let extra ← readNat
    let line ← readNat
    return { operation, sort, args, extra, line : Node }
  let cursor ← get
  require (cursor.position == cursor.bytes.size) "Extra bytes after the circuit"
  return { inputs, states, outputs, effects, clock, nodes }

def decode (bytes : ByteArray) : Except String CheckedCircuit := do
  let (circuit, _) ← readCircuit.run { bytes }
  checkCircuit circuit

def decodeHex (text : String) : Except String CheckedCircuit := do
  let input := text.toUTF8
  require (input.size % 2 == 0) "Odd hexadecimal circuit length"
  let digit (index : Nat) : Except String Nat := do
    if h : index < input.size then
      let value := input[index].toNat
      if value ≥ 48 && value ≤ 57 then return value - 48
      if value ≥ 97 && value ≤ 102 then return value - 87
      throw "Invalid hexadecimal circuit digit"
    else throw "Truncated hexadecimal circuit data"
  let mut bytes := ByteArray.empty
  for index in [:input.size / 2] do
    bytes := bytes.push (UInt8.ofNat ((← digit (2 * index)) * 16 + (← digit (2 * index + 1))))
  decode bytes

def decodeFrame (fields : Array Field) (json : Lean.Json) : Except String (Array Value) :=
  fields.mapM fun field => do
    match field.sort with
    | .bits width => return .bits width (BitVec.ofInt width (← json.getObjValAs? Int field.name))
    | .memory addressWidth width =>
      let words ← json.getObjValAs? (Array Int) field.name
      require (words.size == 2 ^ addressWidth) ("Memory word count mismatch: " ++ field.name)
      return .memory addressWidth width (words.map (BitVec.ofInt width))
    | .boolean => throw "Boolean hardware input/state is unsupported"

def encodeOutputs (circuit : Circuit) (values : Array Value) : Except String Lean.Json := do
  let mut fields := []
  for index in [:circuit.outputs.size] do
    let output ← arrayAt circuit.outputs index
    if !output.observation then fields := fields ++ [(output.name, (← arrayAt values index).toJson)]
  return Lean.Json.mkObj fields

def encodeEffects (circuit : Circuit) (values : Array Value) : Except String Lean.Json := do
  let mut events := #[]
  for index in [:circuit.effects.size] do
    let effect ← arrayAt circuit.effects index
    if ← (← arrayAt values effect.enable).nonzero then
      let kind := if effect.kind == 0 then "assert" else if effect.kind == 1 then "assume"
        else if effect.kind == 2 then "cover" else "print"
      let mut fields := [("index", Lean.toJson index), ("kind", Lean.toJson kind)]
      if let some fd := effect.fileDescriptor then
        fields := fields ++ [("file_descriptor", Lean.toJson fd), ("stream", Lean.toJson effect.stream)]
      if let some column := effect.condition then fields := fields ++ [("condition", (← arrayAt values column).toJson)]
      if let some column := effect.arguments then fields := fields ++ [("arguments", (← arrayAt values column).toJson)]
      events := events.push (Lean.Json.mkObj fields)
  return Lean.Json.arr events

def unwrap (value : Except String α) : IO α :=
  match value with
  | .ok result => pure result
  | .error message => throw (IO.userError message)

def runRequest (checked : CheckedCircuit) (request : Lean.Json) : IO Unit := do
  let circuit := checked.data
  let mut state ← unwrap (decodeFrame circuit.states (← unwrap (request.getObjVal? "initial")))
  if !(← unwrap (initialValid circuit.states state)) then throw (IO.userError "Initial state violates known bits")
  let rows ← unwrap (request.getObjValAs? (Array Lean.Json) "trace")
  let mut cycle : Nat := 0
  for row in rows do
    let inputs ← unwrap (decodeFrame circuit.inputs (← unwrap (row.getObjVal? "inputs")))
    let eval (inputs state : Array Value) : IO Evaluation := do
      let result ← unwrap (evaluate circuit inputs state)
      if !result.outputDefined || !result.transitionDefined then throw (IO.userError "Undefined operation")
      if !result.checksValid then throw (IO.userError "Assertion or assumption failed")
      return result
    let (pre, post, nextState) ← match circuit.clock with
      | none => do
        let result ← eval inputs state
        pure (result.outputs, result.outputs, result.nextState)
      | some index => do
        if h : index < inputs.size then
          let low ← eval (inputs.set index (.bits 1 0)) state
          let high ← eval (inputs.set index (.bits 1 1)) low.nextState
          pure (low.outputs, high.outputs, high.nextState)
        else throw (IO.userError "Clock index outside input frame")
    let mut fields := [("cycle", Lean.toJson cycle), ("pre", ← unwrap (encodeOutputs circuit pre)),
                       ("post", ← unwrap (encodeOutputs circuit post))]
    if !circuit.effects.isEmpty then
      fields := fields ++ [("effects_pre", ← unwrap (encodeEffects circuit pre)),
                           ("effects_post", ← unwrap (encodeEffects circuit post))]
    IO.println (Lean.Json.mkObj fields).compress
    state := nextState
    cycle := cycle + 1

end Chisel2Lean.Functional
