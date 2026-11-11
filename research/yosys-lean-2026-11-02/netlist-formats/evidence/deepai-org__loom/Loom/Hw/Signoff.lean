-- Copyright (c) 2026 Kevin Baragona
-- SPDX-License-Identifier: Apache-2.0
import Loom.Hw.Hierarchy

/-!
# Technology-neutral, fail-closed signoff evidence

This vocabulary covers import equivalence, synthesis equivalence, STA/CDC,
and layout extraction without teaching Loom any vendor or tool semantics.
Reports are external evidence.  Exact ordered requirement coverage, artifact
hashes, assumptions, and identified tool runs prevent a successful-looking
partial or stale report from becoming signoff.
-/

namespace Loom.Hw.Signoff

inductive Stage where
  | importGeneration
  | originalToLoomRtl
  | loomModelToEmittedRtl
  | emittedRtlToSynthesizedNetlist
  | staticTiming
  | clockDomainCrossing
  | netlistToLayoutExtraction
  | layoutVersusSchematic
  deriving Repr, DecidableEq, BEq

inductive Status where
  | pass
  | fail
  | skip
  deriving Repr, DecidableEq, BEq

namespace Status

def render : Status → String
  | .pass => "PASS"
  | .fail => "FAIL"
  | .skip => "SKIP"

end Status

structure Artifact where
  role : String
  path : String
  sha256 : String
  deriving Repr, DecidableEq, BEq

namespace Artifact

private def sha256Like (digest : String) : Bool :=
  digest.length == 64 && digest.toList.all fun character =>
    character.isDigit || ('a' ≤ character && character ≤ 'f') ||
      ('A' ≤ character && character ≤ 'F')

def validB (artifact : Artifact) : Bool :=
  !artifact.role.isEmpty && !artifact.path.isEmpty && sha256Like artifact.sha256

end Artifact

structure Assumption where
  name : String
  statement : String
  deriving Repr, DecidableEq, BEq

structure Requirement where
  id : String
  stage : Stage
  description : String
  required : Bool := true
  inputs : List String
  outputs : List String
  deriving Repr, DecidableEq, BEq

namespace Requirement

def validB (requirement : Requirement) : Bool :=
  !requirement.id.isEmpty && !requirement.description.isEmpty &&
    !requirement.inputs.isEmpty && Inventory.uniqueB requirement.inputs &&
    Inventory.uniqueB requirement.outputs

end Requirement

structure ToolRun where
  adapter : String
  tool : String
  version : String
  runId : String
  invocation : List String
  deriving Repr, DecidableEq, BEq

namespace ToolRun

def complete (run : ToolRun) : Bool :=
  !run.adapter.isEmpty && !run.tool.isEmpty && !run.version.isEmpty &&
    !run.runId.isEmpty && !run.invocation.isEmpty

end ToolRun

structure Plan where
  name : String
  artifacts : List Artifact
  requirements : List Requirement
  assumptions : List Assumption := []

namespace Plan

def validB (plan : Plan) : Bool :=
  !plan.name.isEmpty && !plan.artifacts.isEmpty &&
    Inventory.uniqueB (plan.artifacts.map (·.role)) &&
    plan.artifacts.all Artifact.validB &&
    !plan.requirements.isEmpty &&
    Inventory.uniqueB (plan.requirements.map (·.id)) &&
    plan.requirements.all fun requirement =>
      requirement.validB &&
      requirement.inputs.all (plan.artifacts.map (·.role)).contains &&
      requirement.outputs.all (plan.artifacts.map (·.role)).contains &&
    Inventory.uniqueB (plan.assumptions.map (·.name)) &&
    plan.assumptions.all fun assumption =>
      !assumption.name.isEmpty && !assumption.statement.isEmpty

end Plan

structure Result where
  requirement : Requirement
  status : Status
  detail : String
  run : ToolRun
  artifacts : List Artifact
  assumptions : List Assumption := []
  deriving Repr, DecidableEq, BEq

namespace Result

def validFor (plan : Plan) (result : Result) : Bool :=
  !result.detail.isEmpty && result.run.complete &&
    result.artifacts == plan.artifacts &&
    Inventory.uniqueB (result.assumptions.map (·.name)) &&
    result.assumptions.all fun assumption =>
      !assumption.name.isEmpty && !assumption.statement.isEmpty

end Result

/-- Results must cover the plan's exact ordered requirements. This prevents a
backend from omitting a difficult module or stage while reporting PASS for the
checks it happened to run. -/
structure Report (plan : Plan) where
  results : List Result
  coverage : results.map (·.requirement) = plan.requirements

namespace Report

def complete {plan : Plan} (report : Report plan) : Bool :=
  plan.validB && report.results.all fun result =>
    result.validFor plan &&
      (if result.requirement.required then result.status == .pass
       else result.status != .fail)

def passed {plan : Plan} (report : Report plan) : Bool :=
  report.complete && report.results.all (·.status == .pass)

end Report

/-- Per-module RTL equivalence is a specialization of the generic signoff
plan, retaining the exact original and Loom-emitted artifacts plus any miter
or log emitted by the adapter. -/
def rtlEquivalencePlan (moduleName : String) (original emitted log : Artifact)
    (assumptions : List Assumption := []) : Plan where
  name := moduleName ++ ".rtl_equivalence"
  artifacts := [original, emitted, log]
  assumptions := assumptions
  requirements :=
    [{ id := moduleName ++ ".original_to_loom_rtl"
       stage := .originalToLoomRtl
       description := "original RTL and Loom-emitted RTL are sequentially equivalent under the named assumptions"
       inputs := [original.role, emitted.role]
       outputs := [log.role] }]

end Loom.Hw.Signoff
