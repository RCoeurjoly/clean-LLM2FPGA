#!/usr/bin/env python3
"""Curated classification of the verified upstream command census.

Run after scan_upstream.py. Interpretations are source-based, not claims that an
arbitrary input design successfully exports. Line numbers refer to pinned main.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
REF = "63eecf3ddca2f143750f11e37ebd18ecb16b51c7"
registry = json.loads((OUT / "source-registry-main.json").read_text())
commands = {x["command"]: x for x in registry["registrations"]}

def source(path, line=None):
    return f"https://github.com/YosysHQ/yosys/blob/{REF}/{path}" + (f"#L{line}" if line else "")

def backend(command, format, category, semantics, constraints, variants=(), extra_sources=()):
    r = commands[command]
    return dict(command=command, format=format, category=category,
                semantics=semantics, constraints=constraints, variants=list(variants),
                available_in=["v0.70", "main"], built_in=True,
                registration_source=source(r["path"], r["registration_line"]),
                help_source=source(r["path"], r["help_line"]), blob_sha=r["blob_sha"],
                extra_sources=[source(*x) for x in extra_sources])

functional_constraints = (
    "Shared Functional IR requires normalized global-clock FFs (FfData.has_gclk), not ordinary clocked FFs; "
    "source recommends async2sync or clk2fflogic. Collect memories with memory_collect; write ports must "
    "be asynchronous and read ports unclocked (memory_nordff). Memories without read ports are rejected. "
    "Undriven/multiply-driven nets, combinational loops, unknown cell kinds and $check are rejected; "
    "$check requires chformal -lower. Flatten/resolve hierarchy and lower processes before treating each "
    "selected module as a standalone function. X collision values are treated as don't cares."
)

backends = [
    backend("write_rtlil", "RTLIL text", "internal structural IR",
            "Serializes Yosys internal design objects, including processes, memories, cells, hierarchy, attributes and X/Z constants; consumers still need RTLIL cell semantics.",
            "No requirement to lower processes or memories. Main has Twine identity/replayable printing changes since v0.70; pin version and dialect.",
            ["-selected", "-sort", "main: -readable loses Twine identity/replayability", "main: -small omits name comments"],
            [("backends/rtlil/rtlil_backend.cc", 384)]),
    backend("write_json", "Yosys JSON netlist", "structural netlist",
            "Hierarchy, cells, parameters, attributes, ports, memories and bit connections; optional AIG models for supported cell types.",
            "Processes are explicitly rejected: run proc. A syntactically present cell instance is not a definition of that cell's behavior. Memory/cell semantics need an interpreter.",
            ["-aig", "-compat-int", "-selected", "-noscopeinfo", "json pass exports selected objects"],
            [("backends/json/json.cc", 154)]),
    backend("write_verilog", "Verilog; SystemVerilog output variant", "HDL netlist",
            "Netlist with expressions/always blocks for recognized internal cells and instances for external cells; hierarchy and memories can be emitted.",
            "Help requires proc: RTLIL processes cannot always be mapped directly and trigger a warning. Unmapped/library instances still require external definitions. -sv emits constructs such as always_comb, not a distinct backend.",
            ["-sv", "-noexpr retains internal cells", "-blackboxes", "-extmem emits readmemh .mem initialization sidecars"]),
    backend("write_blif", "BLIF; Yosys extended BLIF", "structural logic netlist",
            "Boolean logic and latches where recognized; other cell/module instances use .subckt or .gate references.",
            "Unmapped RTLIL processes and memories are rejected. -conn/-attr/-param/-cname and related extensions are nonstandard; help advises another suffix for custom consumers.",
            ["-gates", "-icells", "-conn", "-attr", "-param", "-cname", "-iname/-iattr", "-blackbox", "-gatesi .gateinit"],
            [("backends/blif/blif.cc", 720)]),
    backend("write_edif", "EDIF", "technology netlist",
            "Structural cell and wire connectivity in the Xilinx place and route EDIF flavor.",
            "Unmapped processes/memories rejected; cells require matching library definitions. X bits may be exported as zero with a warning. Other EDIF consumers may need adaptation.",
            ["-attrprop", "-keep", "-pvector", "-lsbidx", "GND/VCC constant-driver choices"],
            [("backends/edif/edif.cc", 211), ("backends/edif/edif.cc", 517)]),
    backend("write_spice", "SPICE subcircuit netlist", "technology netlist",
            "Structural instances/subcircuits and connectivity, including chosen power/ground names.",
            "Unmapped processes/memories rejected. Cell/subcircuit models are external; missing blackbox declarations cause guessed port order. Does not synthesize transistor models for arbitrary RTLIL cells.",
            ["-big_endian", "-neg/-pos", "-buf", "-nc_prefix", "-inames"],
            [("backends/spice/spice.cc", 84), ("backends/spice/spice.cc", 226)]),
    backend("write_firrtl", "FIRRTL", "hardware IR",
            "Recognized coarse cells, modules and blackbox extmodules translated into FIRRTL.",
            "Subset only: rejects inout module ports, unsupported cells, negative-edge FFs, initialized memories, nonzero memory offsets, clocked reads, asynchronous/negedge writes and complex write enables. Warns unsupported register initial values. Automatically runs pmuxtree/bmuxmap/demuxmap/bwmuxmap.",
            [], [("backends/firrtl/firrtl.cc", 566), ("backends/firrtl/firrtl.cc", 921), ("backends/firrtl/firrtl.cc", 1007)]),
    backend("write_intersynth", "InterSynth netlist", "coarse grain interconnect netlist",
            "Cell types, connection types and word-wide netlists for coarse-grain example-driven interconnect synthesis.",
            "Rejects unprocessed memories/processes, unknown cell types, composite signals and signals that are not word-wide. Cell interfaces must be known; -lib adds Verilog/RTLIL library definitions.",
            ["-notypes", "-lib", "-selected (whole modules)"],
            [("backends/intersynth/intersynth.cc", 33), ("backends/intersynth/intersynth.cc", 142)]),
    backend("write_aiger", "AIGER binary; ASCII AAG", "Boolean transition system",
            "Flattened AND/inverter graph with simple FF state; asserts become bad-state properties and assumes invariant constraints. Initstate/nondeterministic formal cells have special handling.",
            "Requires flattened and gate-lowered design; no unprocessed memories/processes. Supported FFs include $_FF_ and $_DFF_[NP]_; arbitrary clocks/resets/enables must be normalized before interpreting an AIGER transition as a clocked hardware step. Unsupported cell kinds error. -ywmap currently requires -zinit.",
            ["-ascii", "-zinit", "-miter", "-symbols", "-map/-vmap port/latch maps", "-ywmap JSON witness map"],
            [("backends/aiger/aiger.cc", 234), ("backends/aiger/aiger.cc", 1021)]),
    backend("write_aiger2", "AIGER binary", "combinational Boolean model",
            "Experimental newer writer directly ingests a larger supported subset of combinational coarse cells/gates and can traverse hierarchy with -flatten.",
            "Not a drop-in sequential replacement for write_aiger: known_ops is combinational, no FF/memory operations. Unknown/blackbox cells fail; -flatten respects keep_hierarchy. Source has TODOs for inout, undriven and zero-width cases. Help generates its unsupported-cell list dynamically; literal extraction retains %s and raw C++.",
            ["-strash", "-flatten"], [("backends/aiger2/aiger.cc", 36), ("backends/aiger2/aiger.cc", 127)]),
    backend("write_xaiger", "XAIGER / extended AIGER", "ABC mapping interchange",
            "AIG plus ABC box information and optional simple FFs; non-AIG/non-box cells become pseudo-inputs and pseudo-outputs. Whiteboxes can use $abc9_holes definitions.",
            "A cut-and-box mapping representation; not automatically a complete standalone semantic model. -dff enables $_DFF_[NP]_. Symbol map references are valid only in the writing Yosys run unless debug string maps are selected.",
            ["-ascii", "-dff", "-map", "-map-strings"]),
    backend("write_xaiger2", "XAIGER with h/a extensions", "ABC mapping interchange",
            "Newer combinational AIG writer with hierarchical/opaque-box information for ABC.",
            "Same combinational known_ops core as aiger2, with boxed unsupported logic rather than a general clocked model. -mapping_prep mutates/removes exported cells; -mapping_prep and -map2 conflict with -flatten.",
            ["-strash", "-flatten", "-map2 reintegration map", "-mapping_prep (mutating)"]),
    backend("write_smt2", "SMT-LIBv2", "formal transition system",
            "Per-module state sort, hierarchy relation, initial predicate, transition relation and assertion/assumption/cover predicates; BitVec arithmetic and ArraysEx memories by default.",
            "Lower processes. Unmap sync reset/enables with dffunmap; async FFs need async2sync; dffunmap or clk2fflogic, and latches need clk2fflogic. Read-port registers must not be merged into memories (memory -nordff / memory_nordff). -nobv implies -nomem, -stbv disables array memories. Unknown cells fail unless explicitly supplied as SMT blackboxes; $live/$fair/$equiv are rejected. A proof driver must assert hierarchy/init/transition predicates correctly; clock normalization matters for multiple clocks.",
            ["-stbv", "-stdt", "-nobv", "-nomem", "-wires", "-tpl", "-solver-option"],
            [("backends/smt2/smt2.cc", 930), ("backends/smt2/smt2.cc", 1697), ("backends/smt2/smt2.cc", 1108)]),
    backend("write_btor", "BTOR2", "formal transition system",
            "Bit-vector/array state, init/next transitions, bad properties and constraints. Despite command/help spelling BTOR, emitted sort/state/next syntax is BTOR2.",
            "One top module; flatten hierarchy/resolve unknown cells. Normalize FF types as with SMT2 (dffunmap, async2sync or clk2fflogic); no synchronous memory reads (memory_nordff), no mixed sync/async memory write ports. Explicit error paths for unsupported cells. A hardware proof still requires an agreed clock-step model.",
            ["-s single bad property", "-c covers instead of asserts", "-i auxiliary info", "-ywmap JSON witness map", "-x symbols"],
            [("backends/btor/btor.cc", 806), ("backends/btor/btor.cc", 965)]),
    backend("write_smv", "SMV", "formal transition system",
            "SMV module expressions and next-state equations for a supported subset.",
            "Help says UNDER CONSTRUCTION. Modules with raw memories/processes are skipped with warnings; packed $mem is a FIXME. Supports only selected cell/FF types; error messages require dffunmap, async2sync or clk2fflogic for unsupported FF families. Do not assume arbitrary mapped cells or memory support.",
            ["-verbose", "-tpl"], [("backends/smv/smv.cc", 228), ("backends/smv/smv.cc", 580), ("backends/smv/smv.cc", 770)]),
    backend("write_cxxrtl", "C++ CXXRTL", "simulation code",
            "Executable RTL simulation code and optional header, with sequential state, memories, hierarchy, blackbox interfaces and explicit eval/commit stepping.",
            "Requires driver clock/port interaction and race-free scheduling. Supports processes (normally lowers them unless -noproc); rejects unknown cells, unmarked external blackboxes, multi-driver wires and global-clock processes. C++ simulation model is not a Lean proof artifact.",
            ["-header", "-noflatten", "-noproc", "optimization/debug levels", "CXXRTL C API and VCD runtime are companion interfaces"],
            [("backends/cxxrtl/cxxrtl_backend.cc", 2988), ("backends/cxxrtl/cxxrtl_backend.cc", 3077), ("backends/cxxrtl/cxxrtl_backend.cc", 3199)]),
    backend("write_simplec", "C (simplec)", "simulation/formal-oriented code",
            "Simple C intended for C simulation and C-based verification; separate from CXXRTL and functional C++.",
            "Help says UNDER CONSTRUCTION. Actual eval_cell supports a limited set of fine Boolean gates; unknown cells fail with 'No C model ... FIXME'. Requires a top module. No blanket memory/FF/clock support claim.",
            ["-i8/-i16/-i32/-i64", "-verbose"], [("backends/simplec/simplec.cc", 393), ("backends/simplec/simplec.cc", 511)]),
    backend("write_functional_cxx", "C++ Functional IR", "functional simulation code",
            "Per-selected-module pure evaluation/state functions and initial state using functional C++ runtime; separate from CXXRTL.",
            functional_constraints + " Command help is TODO; use source constraints rather than inferring maturity.",
            [], [("kernel/functional.cc", 559), ("kernel/functional.cc", 610), ("kernel/functional.cc", 644)]),
    backend("write_functional_smt2", "SMT-LIB from Functional IR", "functional formal model",
            "Functional bit-vector/array/state encoding; separate schema from traditional write_smt2 and its SMTBMC metadata contract.",
            functional_constraints + " Help contains only a one-line description.",
            [], [("kernel/functional.cc", 559), ("kernel/functional.cc", 610), ("kernel/functional.cc", 644)]),
    backend("write_functional_rosette", "Rosette-compatible Racket", "functional formal model",
            "#lang rosette/safe output with bit-vector operations, memories and state from Functional IR.",
            functional_constraints,
            ["-provides", "-assoc-list-helpers"], [("kernel/functional.cc", 559), ("kernel/functional.cc", 610)]),
    backend("write_jny", "JNY JSON design metadata", "metadata",
            "JSON metadata, module/cell type summaries, selected connections, attributes and parameters according to misc/jny.schema.json.",
            "Metadata interchange, not a replacement for a complete RTLIL/HDL/formal semantics. Help synopsis says jny, but actual Backend registration creates write_jny; jny pass is a selected-object alternative.",
            ["-no-connections", "-no-attributes", "-no-properties", "jny -o alternative"], [("misc/jny.schema.json", None)]),
    backend("write_table", "Tab-separated ASCII connectivity table", "connectivity report",
            "Six columns: module, cell, type, port, direction, signal; module IO represented as pseudo cells.",
            "Connectivity view only; it omits the full behavioral definitions/parameters needed to interpret arbitrary cells and processes.", []),
]

def auxiliary(command, formats, category, notes, paths=()):
    r = commands.get(command)
    sources = ([source(r["path"], r["registration_line"])] if r else []) + [source(*p) for p in paths]
    return dict(command=command, formats=formats, category=category, notes=notes, sources=sources)

auxiliary_exports = [
    auxiliary("dump", ["RTLIL text"], "alternate command", "Selected RTLIL dump; -outfile/-append output file."),
    auxiliary("json", ["Yosys JSON"], "alternate command", "Selected-object form of write_json, with -o."),
    auxiliary("jny", ["JNY JSON"], "alternate command", "Selected-object metadata form, with -o."),
    auxiliary("fsm_export", ["KISS2"], "hardware subset", "Only extracted $fsm cells; -origenc/-o/-noauto; not a whole-design format."),
    auxiliary("fsm_recode", ["Synopsys Formality set_fsm_* script", "Yosys .fsm/.map encoding map"], "mapping sidecar", "-fm_set_fsm_file / -encfile report old/new FSM state encodings."),
    auxiliary("show", ["Graphviz DOT", "Graphviz render target such as SVG or PostScript"], "visualization", "Yosys emits DOT; other requested formats are rendered by external dot, whose formats are an external dependency."),
    auxiliary("viz", ["Graphviz DOT", "Graphviz rendered graphics"], "visualization", "Dataflow graph; external dot renders non-DOT output."),
    auxiliary("sat", ["DIMACS CNF", "VCD", "WaveJSON"], "SAT problem / witness", "-dump_cnf serializes the configured SAT problem; -dump_vcd/-dump_json serialize a found model/counterexample, not the full circuit."),
    auxiliary("sim", ["VCD", "FST", "AIGER witness AIW", "JSON summary"], "simulation trace", "-vcd/-fst/-aiw/-summary; AIW requires symbol map. FST availability depends on the build. Reading WIT/YW is not an output backend."),
    auxiliary("fst2tb", ["Verilog testbench", "text stimulus .txt"], "testbench", "-tb <name> produces <name>.v and <name>.txt from FST stimuli."),
    auxiliary("test_autotb", ["Verilog testbench", "runtime textual samples"], "test backend", "The 23rd Backend registration, outside backends/. Generates primitive semi-random testbenches; samples are produced when generated HDL runs."),
    auxiliary("icell_liberty", ["Liberty .lib"], "cell interface library", "Interfaces of used internal cells; help explicitly says models are not guaranteed logically sound. Not a sound complete hardware export."),
    auxiliary("abc9_ops", ["ABC LUT library", "ABC box library"], "mapping sidecar", "-write_lut/-write_box dump precomputed mapping libraries. ABC is an external engine; its own arbitrary export commands are not Yosys backends."),
    auxiliary("synthprop", ["synthesizable-property port mapping text"], "mapping sidecar", "-map records generated output property port assignments."),
    auxiliary("linecoverage", ["LCOV", "text report"], "coverage report", "-lcov produces source-location coverage from selection; no circuit semantics."),
    auxiliary("stat", ["statistics JSON", "text report"], "statistics report", "-json writes to log/console; tee can save it. Distinct from write_json."),
    auxiliary("help", ["text reference", "cell model Verilog", "command-documentation JSON", "cell-documentation JSON"], "documentation", "help <cell>+ emits the Verilog model. Undocumented internal -dump-cmds-json/-dump-cells-json write documentation schemas, not netlists.", [("kernel/register.cc", 1136)]),
    auxiliary("select", ["plain-text object selection list"], "selection report", "-write emits the same objects as -list."),
    auxiliary("mutate", ["Yosys mutate-command script", "source-tag list"], "mutation metadata", "-list with -o/-s outputs mutation commands and src metadata."),
    auxiliary("qbfsat", ["SMT-LIBv2 problem", "plain-text solution assignments"], "solver problem / solution", "-dump-final-smt2 and -write-solution; underlying yosys-smtbmc/SMT solver external execution is separate."),
    auxiliary("opensta / sdc_expand", ["expanded SDC constraints", "temporary Tcl script, Verilog netlist and Liberty interfaces"], "external delegated output", "Yosys runs external OpenSTA write_sdc; no registered built-in write_sdc command. Requires OpenSTA and the relevant Yosys components.", [("techlibs/common/opensta.cc", 96), ("techlibs/common/sdc_expand.cc", 117)]),
    auxiliary("verific", ["Verilog netlist dump", "Verilog/SystemVerilog pretty print", "VHDL pretty print"], "optional licensed frontend output", "Only with proprietary Verific support: -d dumps Verific netlist; -pp pretty-prints loaded Verific designs, including -vhdl if supported. Not a write_vhdl backend from arbitrary RTLIL.", [("frontends/verific/verific.cc", 3375), ("frontends/verific/verific.cc", 3408)]),
    auxiliary("write_file", ["arbitrary copied/heredoc text"], "file utility", "Registered as Frontend(=write_file); copies text, does not translate hardware."),
    auxiliary("tee", ["captured command log text"], "file utility", "Routes another command's log to a file; does not add a netlist format."),
    auxiliary("driver flags", ["plain-text log", "Makefile dependency file", "performance JSON"], "driver diagnostics", "-l/-L, -E/--deps-file, --perffile; not hardware semantics.", [("kernel/driver.cc", 322), ("kernel/driver.cc", 338), ("kernel/driver.cc", 349)]),
    auxiliary("debug/reduction/synthesis wrappers", ["already-counted RTLIL or Verilog, plus logs/scripts"], "existing format reuse", "bugpoint, extract -mine, freduce -dump, test_cell HDL dumping, read_verilog AST/Verilog/RTLIL debugging, synth_* output-file switches and ABC temporary artifacts reuse formats. Arbitrary logs, AST debug text, scripts and library/compiler traces are not additional whole-design backends.", [("passes/techmap/extract.cc", 748), ("passes/sat/freduce.cc", 608)]),
    auxiliary("yosys-smtbmc", ["VCD", "Yosys witness YW JSON", "Verilog testbench", "SMTC constraints", "SMT-LIBv2 solver transcript"], "bundled companion executable", "--dump-vcd/--dump-yw/--dump-vlogtb/--dump-smtc; SMTIO --dump-smt2. These are proof traces/problems, not additional registered Yosys design backends.", [("backends/smt2/smtbmc.py", 135), ("backends/smt2/smtio.py", None)]),
    auxiliary("yosys-witness", ["Yosys witness YW JSON", "AIGER witness AIW", "text trace/statistics"], "bundled companion executable", "yw2yw, aiw2yw, yw2aiw, wit2yw convert/transform witnesses using backend map files. Reading BTOR witnesses does not imply writing a BTOR witness format.", [("backends/smt2/witness.py", 87), ("backends/smt2/witness.py", 298), ("backends/smt2/witness.py", 357)]),
]

inventory = dict(
    research_date="2026-11-02", upstream="https://github.com/YosysHQ/yosys",
    stable=dict(tag="v0.70", published_at="2026-10-13T09:20:48Z", commit="28ba3cb926fe376fa05ebfc1b921747d011d4557"),
    main=dict(commit=REF, committed_at="2026-10-29T15:02:43Z"),
    exhaustive_scope="All literal Backend registrations and write_-named registrations in Yosys core source at both pinned commits (backends, frontends, passes, techlibs, kernel), checked against CMake backend directories; all 22 production write_* backends and test_autotb. File-writer candidates and registered-command help were scanned for auxiliary formats. Plugins, external executables and source submodules can define additional unbounded output formats.",
    build_scope="Source inventory of upstream's available components, not a claim that every distributor or selectively configured YOSYS_COMPONENTS binary enables them. No latest-version build or arbitrary-input export suite was run.",
    backend_counts=dict(production_write_backends=22, test_backends=1, all_Backend_registrations=23, extra_write_prefix_utility_frontends=1),
    verification=dict(method="Official GitHub release/commit/tree metadata via connector; official pinned codeload archives; every core C++ source blob independently hashed against GitHub tree entries", main_cpp_files=328, stable_cpp_files=324, blob_mismatches=0, details="archive-validation.json"),
    production_backends=backends, auxiliary_exports=auxiliary_exports,
    absent_builtin_names=["write_lean", "write_lean4", "write_vhdl", "write_btor2", "write_simplecxx", "write_sdc"],
    limitations=[
        "Finite source census, not every arbitrary file emitted by plugins, the shell/Tcl/Python APIs, ABC, Graphviz, OpenSTA or linked proprietary libraries.",
        "Auxiliary output survey includes explicit file options, reports, mapping sidecars, visualizations, solver traces and bundled witness tools; log text/debug AST dumps are not each counted as distinct whole-design interchange languages.",
        "Full raw C++ help bodies and literal help strings are retained in source-registry-*.json. Dynamic help expansion (notably aiger2 supported-cell exclusions) was inspected in source, not executed; %s placeholders are not actual runtime command output.",
        "Model generation is not by itself a proof of the exporter or a Lean-checked equivalence certificate; input domain, cell library, X/Z treatment, clock model and preprocessing must be fixed separately.",
    ],
)
assert {x["command"] for x in backends} == {x["command"] for x in registry["registrations"] if x["registry_type"] == "Backend" and x["command"] != "test_autotb"}
(OUT / "export-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")

lines = ["#+title: Complete finite upstream Yosys export inventory (2026-11-02)",
         "#+startup: overview", "", "* Versions and definition of completeness", "",
         "Latest stable: [[https://github.com/YosysHQ/yosys/releases/tag/v0.70][Yosys 0.70]], published 2026-10-13, commit =28ba3cb926fe376fa05ebfc1b921747d011d4557=.",
         f"Upstream main: [[https://github.com/YosysHQ/yosys/commit/{REF}][{REF}]], committed 2026-10-29.", "",
         inventory["exhaustive_scope"], "", inventory["build_scope"], "",
         "Both snapshots contain the same *22 production write_* backends*, plus =test_autotb= as a 23rd Backend registration. =write_file= is a named Frontend utility, not a design backend.", "",
         "Registry mapping is defined in [[" + source("kernel/register.cc", 574) + "][kernel/register.cc]]; the main [[" + source("backends/CMakeLists.txt") + "][backend build manifest]] lists 18 source directories. =functional= holds three writers, and AIGER/XAIGER each have old/new implementations.", "",
         "* Every production backend", "",
         "Each entry names actual commands, records the format, and states semantics and restrictions. These are source-derived limits, not a promise that any arbitrary design exports.", ""]
for b in backends:
    lines += ["** =" + b["command"] + "= — " + b["format"], "",
              "Category: " + b["category"] + ".", "", b["semantics"], "", b["constraints"], ""]
    if b["variants"]:
        lines += ["Variants/options: " + "; ".join(b["variants"]) + ".", ""]
    lines += ["Evidence: [[" + b["registration_source"] + "][registration]], [[" + b["help_source"] + "][help]]" + "".join(", [[" + s + "][source detail]]" for s in b["extra_sources"]) + ".", ""]
lines += ["* Additional explicit outputs and nearby commands", "",
          "These are included to prevent conflating all file output with full hardware models. A wrapper that emits an already-counted format does not add another backend.", ""]
for a in auxiliary_exports:
    lines += ["** =" + a["command"] + "=", "", "Formats: " + "; ".join(a["formats"]) + ". Category: " + a["category"] + ".", "", a["notes"], "", "Evidence: " + ", ".join("[[" + s + "][source]]" for s in a["sources"]) + ".", ""]
lines += ["* Limits, naming traps, and evidence artifacts", "",
          "There is no registered built-in =write_lean=, =write_lean4=, =write_vhdl=, =write_btor2=, =write_simplecxx= or =write_sdc= in these snapshots. The real commands are =write_btor= (BTOR2), =write_simplec= (C), =write_cxxrtl= (C++), and =write_functional_cxx= (another C++ schema). Verific's optional VHDL pretty-printer is explicitly distinguished above.", ""]
lines += ["- " + s for s in inventory["limitations"]]
lines += ["", "Artifacts:", "", "- [[file:export-inventory.json][Machine-readable curated inventory]].",
          "- [[file:registration-census.json][Exact main/stable Backend and write_* command census]].",
          "- [[file:source-registry-main.json][Main registrations, full raw help methods, file-writer candidates and blob checksums]].",
          "- [[file:source-registry-stable.json][Stable registrations, full raw help methods, file-writer candidates and blob checksums]].",
          "- [[file:backend-help-main.org][Main backend help literals]] and [[file:backend-help-stable.org][stable backend help literals]]; %s is dynamic source help, not evaluated runtime output.",
          "- [[file:github-evidence.json][GitHub connector release/commit/tree evidence]].",
          "- [[file:github-stable-source-tree.json][Stable core C++ tree evidence]] and [[file:archive-validation.json][archive/blob validation and help differences]].",
          "- [[file:local-yosys.org][Local sampled Yosys build and functional-help observations]].",
          "- [[file:scan_upstream.py][Reproducible scanner]] and [[file:classify_inventory.py][curated classification generator]].", ""]
(OUT / "inventory.org").write_text("\n".join(lines))
print(f"Wrote {len(backends)} production backends and {len(auxiliary_exports)} auxiliary output entries.")
