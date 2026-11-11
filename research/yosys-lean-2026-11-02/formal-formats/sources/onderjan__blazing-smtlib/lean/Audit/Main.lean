module

import Lean
import Audit.Declarations

/-!
# The audit

The executable `audit`, run in the package's directory.

`lake exe audit` is the full audit, its steps one after the other:

1. `lake clean`, so that the rest runs on a clean build (left out with `--incremental`);
2. `lake build` of the default targets, the executables and the benchmarks, which also runs what
   the facts pin: every theorem's axioms (`#print axioms`), the naming rule
   (`BSmtLib.Facts.Names`), the adapters (`#adapters`, `#trusts`) and the library's assertions
   (`BSmtLib.Facts.Asserts`);
3. the declaration audit (`Audit.Declarations`): the axioms of every declaration, the constructs
   that bypass the kernel or the compiler, `debug.skipKernelTC`, and the panics that the
   library's compiled code reaches;
4. `lake env leanchecker` on the modules `leancheckerRoots`: Lean's kernel re-checks them from
   their `.olean` files;
5. nanoda, a second kernel, on the modules `nanodaRoots`: `SCRIPT DIR ROOTS…`, `SCRIPT` being the
   script given by `--nanoda SCRIPT` or by the environment variable `AUDIT_NANODA`, and `DIR`
   this directory. Without it the audit stops before it begins.

`lake exe audit --fast` is the declaration audit alone, over the build there is.

A failed build stops the audit; after it, every step runs and the audit fails if one of them
does. The environment variable `LAKE` names the Lake command, `lake` if unset.

`--jobs N` runs every step with at most `N` threads: the build and `leanchecker` with
`LEAN_NUM_THREADS=N`, nanoda with `-j N`. `leanchecker` imports the whole import closure of every
module it re-checks, one environment per thread, so its peak memory grows with the threads;
fewer threads trade time for memory. Without `--jobs` the audit chooses `N` itself, so that the
threads fit into three quarters of the memory available at its start with `bytesPerThread` each,
at least one and at most the processors there are (`defaultJobs`); it says what it chose.
-/

namespace Audit.Main

/-- The modules that `leanchecker` re-checks: the roots of the four libraries, the library, the
specification and the facts of both. -/
def leancheckerRoots : List String :=
  ["BSmtLib", "BSmtLibSpec", "BSmtLibSpec.Facts", "BSmtLib.Facts", "BSmtLib.Facts.Names",
    "BSmtLib.Facts.Asserts"]

/-- The modules that nanoda re-checks: the roots of the four libraries, the library, the
specification and the facts of both. -/
def nanodaRoots : List String :=
  ["BSmtLib", "BSmtLibSpec", "BSmtLibSpec.Facts", "BSmtLib.Facts.Lexer", "BSmtLib.Facts.Parser",
    "BSmtLib.Facts.Reader", "BSmtLib.Facts.PublicApi", "BSmtLib.Facts.Asserts"]

/-- The targets that `lake build` builds beside the default ones: every executable, so that the
declaration audit finds every module of the package built. -/
def extraTargets : List String :=
  ["reexports", "rust-items", "bench-parse", "bench-fused-parser", "audit"]

/-- The memory a thread of the audit's steps is allowed: well above what a thread of
`leanchecker`, the most demanding step, takes on this package. -/
def bytesPerThread : Nat := 6 * 1024 * 1024 * 1024

/-- The memory available now, in bytes, from `/proc/meminfo`; none where it cannot be read. -/
def availableBytes : IO (Option Nat) := do
  let text ← try IO.FS.readFile "/proc/meminfo" catch _ => return none
  for line in text.splitOn "\n" do
    if line.startsWith "MemAvailable:" then
      -- the line is `MemAvailable:` and a number of KiB
      if let some kib := (String.ofList (line.toList.filter Char.isDigit)).toNat? then
        return some (kib * 1024)
  return none

/-- The number of processors, from `nproc`; none where it cannot be run. -/
def processors : IO (Option Nat) := do
  let output ← try IO.Process.output { cmd := "nproc" } catch _ => return none
  if output.exitCode != 0 then return none
  return (String.ofList (output.stdout.toList.filter Char.isDigit)).toNat?

/-- The threads to use when the command line does not say: as many as fit into three quarters of
the available memory with `bytesPerThread` each, at least one and at most the processors; two
where the memory or the processors cannot be read. With the reason, to be printed. -/
def defaultJobs : IO (Nat × String) := do
  match ← availableBytes, ← processors with
  | some available, some cpus =>
    let fit := available * 3 / 4 / bytesPerThread
    let jobs := max 1 (min fit cpus)
    return (jobs, s!"{available / (1024 * 1024 * 1024)} GiB available, \
      {bytesPerThread / (1024 * 1024 * 1024)} GiB per thread, {cpus} processors")
  | _, _ => return (2, "the available memory or the processors could not be read")

/-- Runs `cmd` with `args` and the environment `env`, its output shown as it comes; whether it
succeeded. -/
def step (cmd : String) (args : List String) (env : Array (String × Option String) := #[]) :
    IO Bool := do
  IO.println s!"==> {cmd} {" ".intercalate args}"
  let child ← IO.Process.spawn { cmd, args := args.toArray, env }
  let code ← child.wait
  unless code == 0 do IO.eprintln s!"==> failed with exit code {code}: {cmd} {" ".intercalate args}"
  return code == 0

/-- What the command line asks for. -/
structure Options where
  /-- The declaration audit alone, over the build there is. -/
  fast : Bool := false
  /-- No `lake clean` before the build. -/
  incremental : Bool := false
  /-- The script that runs nanoda. -/
  nanoda : Option String := none
  /-- The most threads a step may use; chosen by `defaultJobs` if none. -/
  jobs : Option Nat := none

/-- The options of the command line, or none if it is malformed. -/
def parseOptions : List String → Options → Option Options
  | [], options => some options
  | "--fast" :: rest, options => parseOptions rest { options with fast := true }
  | "--incremental" :: rest, options => parseOptions rest { options with incremental := true }
  | "--nanoda" :: script :: rest, options => parseOptions rest { options with nanoda := script }
  | "--jobs" :: n :: rest, options =>
    match n.toNat? with
    | some jobs => if jobs == 0 then none else parseOptions rest { options with jobs := jobs }
    | none => none
  | _, _ => none

/-- Runs the full audit; the exit code is 1 if a step failed. -/
def run (options : Options) : IO UInt32 := do
  let script := options.nanoda <|> (← IO.getEnv "AUDIT_NANODA")
  let some nanoda := script
    | IO.eprintln "the full audit runs nanoda: give the script that runs it with \
        `--nanoda SCRIPT` or the environment variable AUDIT_NANODA, \
        or run `lake exe audit --fast` for the declaration audit alone"
      return 2
  let lake := (← IO.getEnv "LAKE").getD "lake"
  -- the thread limit, for the Lean processes and for nanoda
  let jobs ← match options.jobs with
    | some jobs =>
      IO.println s!"==> {jobs} threads, as given"
      pure jobs
    | none => do
      let (jobs, reason) ← defaultJobs
      IO.println s!"==> {jobs} threads ({reason}; set it with --jobs N)"
      pure jobs
  let env := #[("LEAN_NUM_THREADS", some (toString jobs))]
  let nanodaJobs := ["-j", toString jobs]
  unless options.incremental do
    unless ← step lake ["clean"] env do return 1
  unless ← step lake ["build"] env do return 1
  unless ← step lake ("build" :: extraTargets) env do return 1
  let mut failed : Array String := #[]
  IO.println "==> the declaration audit"
  unless (← Audit.Declarations.run) == 0 do failed := failed.push "the declaration audit"
  unless ← step lake (["env", "leanchecker"] ++ leancheckerRoots) env do
    failed := failed.push "leanchecker"
  let dir := (← IO.currentDir).toString
  unless ← step nanoda (nanodaJobs ++ dir :: nanodaRoots) env do failed := failed.push "nanoda"
  if failed.isEmpty then
    IO.println "==> the audit passed"
    return 0
  else
    IO.eprintln s!"==> the audit failed: {", ".intercalate failed.toList}"
    return 1

end Audit.Main

/-- Runs the audit: `audit [--fast] [--incremental] [--nanoda SCRIPT] [--jobs N]`. -/
public def main (args : List String) : IO UInt32 :=
  match Audit.Main.parseOptions args {} with
  | some options => if options.fast then Audit.Declarations.run else Audit.Main.run options
  | none => do
    IO.eprintln "usage: audit [--fast] [--incremental] [--nanoda SCRIPT] [--jobs N]"
    return 2
