-- Generated from locked Yosys FunctionalIR. Fixed circuit; explicit initial state.
import Lean
set_option linter.unusedVariables false
set_option maxRecDepth 65536
set_option maxHeartbeats 0
namespace TinyStoriesProbe
structure Inputs where
  i0 : BitVec 1
  i1 : BitVec 1
  i2 : BitVec 1
  i3 : BitVec 1
  i4 : BitVec 4
  i5 : BitVec 32
  i6 : BitVec 32
  i7 : BitVec 4

structure State where
  s0 : BitVec 1
  s1 : BitVec 32
  s2 : BitVec 32
  s3 : BitVec 4
  s4 : (BitVec 4 → BitVec 32)
  s5 : BitVec 1
  s6 : BitVec 1
  s7 : BitVec 32
  s8 : BitVec 32
  s9 : BitVec 1
  s10 : BitVec 1

structure Outputs where
  o0 : BitVec 32
  o1 : BitVec 1

structure Evaluation where
  outputs : Outputs
  nextState : State
  outputDefined : Bool
  transitionDefined : Bool

def evaluate (inputs : Inputs) (state : State) : Evaluation :=
  let n0 : BitVec 1 := inputs.i0
  let n1 : BitVec 1 := inputs.i1
  let n2 : BitVec 1 := (~~~n1)
  let n3 : BitVec 1 := inputs.i2
  let n4 : BitVec 1 := (n2 &&& n3)
  let n5 : BitVec 1 := inputs.i3
  let n6 : BitVec 1 := (n4 &&& n5)
  let n7 : BitVec 1 := (1#1)
  let n8 : Bool := (n6 == n7)
  let n9 : BitVec 32 := (4294967295#32)
  let n10 : BitVec 32 := (0#32)
  let n11 : BitVec 32 := (if n8 then n9 else n10)
  let n12 : BitVec 32 := inputs.i6
  let n13 : BitVec 32 := inputs.i5
  let n14 : BitVec 1 := (1#1)
  let n15 : Bool := (n6 == n14)
  let n16 : BitVec 32 := (if n15 then n13 else n12)
  let n17 : BitVec 4 := inputs.i7
  let n18 : BitVec 4 := inputs.i4
  let n19 : BitVec 1 := (1#1)
  let n20 : Bool := (n6 == n19)
  let n21 : BitVec 4 := (if n20 then n18 else n17)
  let n22 : (BitVec 4 → BitVec 32) := state.s4
  let n23 : BitVec 4 := state.s3
  let n24 : BitVec 32 := (n22 n23)
  let n25 : BitVec 32 := state.s1
  let n26 : BitVec 1 := state.s0
  let n27 : BitVec 2 := (n0 ++ n26)
  let n28 : BitVec 2 := (2#2)
  let n29 : Bool := (n27 == n28)
  let n30 : BitVec 1 := (1#1)
  let n31 : BitVec 1 := (0#1)
  let n32 : BitVec 1 := (if n29 then n30 else n31)
  let n33 : BitVec 1 := (1#1)
  let n34 : Bool := (n32 == n33)
  let n35 : BitVec 32 := (0#32)
  let n36 : BitVec 32 := (if n34 then n25 else n35)
  let n37 : BitVec 32 := (~~~n36)
  let n38 : BitVec 32 := (n24 &&& n37)
  let n39 : BitVec 32 := state.s2
  let n40 : BitVec 32 := (n39 &&& n36)
  let n41 : BitVec 32 := (n38 ||| n40)
  let n42 : (BitVec 4 → BitVec 32) := (fun address => if address == n23 then n41 else n22 address)
  let n43 : BitVec 32 := state.s8
  let n44 : BitVec 32 := state.s7
  let n45 : BitVec 1 := state.s5
  let n46 : BitVec 2 := (n45 ++ n0)
  let n47 : BitVec 2 := (1#2)
  let n48 : Bool := (n46 == n47)
  let n49 : BitVec 1 := (1#1)
  let n50 : BitVec 1 := (0#1)
  let n51 : BitVec 1 := (if n48 then n49 else n50)
  let n52 : BitVec 1 := (1#1)
  let n53 : Bool := (n51 == n52)
  let n54 : BitVec 32 := (if n53 then n44 else n43)
  let n55 : BitVec 32 := (n42 n18)
  let n56 : BitVec 1 := (~~~n5)
  let n57 : BitVec 1 := (n3 &&& n56)
  let n58 : BitVec 1 := (1#1)
  let n59 : Bool := (n57 == n58)
  let n60 : BitVec 32 := (if n59 then n55 else n54)
  let n61 : BitVec 1 := (1#1)
  let n62 : Bool := (n1 == n61)
  let n63 : BitVec 32 := (0#32)
  let n64 : BitVec 32 := (if n62 then n63 else n60)
  let n65 : BitVec 1 := (~~~n1)
  let n66 : BitVec 1 := (n65 &&& n3)
  let n67 : BitVec 1 := state.s10
  let n68 : BitVec 1 := state.s9
  let n69 : BitVec 1 := state.s6
  let n70 : BitVec 2 := (n69 ++ n0)
  let n71 : BitVec 2 := (1#2)
  let n72 : Bool := (n70 == n71)
  let n73 : BitVec 1 := (1#1)
  let n74 : BitVec 1 := (0#1)
  let n75 : BitVec 1 := (if n72 then n73 else n74)
  let n76 : BitVec 1 := (1#1)
  let n77 : Bool := (n75 == n76)
  let n78 : BitVec 1 := (if n77 then n68 else n67)
  { outputs := { o0 := n54, o1 := n78 }, nextState := { s0 := n0, s1 := n11, s2 := n16, s3 := n21, s4 := n42, s5 := n0, s6 := n0, s7 := n64, s8 := n54, s9 := n66, s10 := n78 },
    outputDefined := true, transitionDefined := true }

def step (inputs : Inputs) (state : State) : Outputs × State :=
  let r := evaluate inputs state
  (r.outputs, r.nextState)

def initialValid (state : State) : Bool :=
  ((state.s0 &&& (1#1)) == (1#1)) &&
  ((state.s5 &&& (1#1)) == (1#1)) &&
  ((state.s6 &&& (1#1)) == (1#1)) &&
  ((state.s7 &&& (4294967295#32)) == (0#32)) &&
  ((state.s9 &&& (1#1)) == (0#1))
end TinyStoriesProbe
