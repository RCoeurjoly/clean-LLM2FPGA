"""
scripts/btor2_ir.py -- generic BTOR2 (bv-only subset) parser + Lean/BitVec
codegen for WS4(d) (spec-week2.md WS4(d): "node-by-node transcribe real
benchmarks_btor2_bv instances' init/next/bad, unroll to depth k").

Supported grammar (exactly the ops that occur in the sub-frontier instances
this pass mined -- anything else raises Btor2UnsupportedError so the caller
can log-and-skip rather than force a translation, matching every other WS4
family's "restrict to a closed grammar, skip what falls outside it" rule):
  sort bitvec <n>
  input <sort>            state <sort>
  init <sort> <state> <val>      next <sort> <state> <val>
  bad <val>
  const <sort> <bits>     constd <sort> <dec>
  not <sort> <a>          neg <sort> <a>
  and/or/xor <sort> <a> <b>      add/sub/mul/udiv/urem <sort> <a> <b>
  eq/neq/ult/ugte/slt <sort> <a> <b>
  sext/uext <sort> <a> <extbits>
  ite <sort> <c> <t> <e>
No array sorts, no negative (NOT-shorthand) literal references -- neither
appears in the instances this generator targets (confirmed by grep before
use); both raise Btor2UnsupportedError if encountered.

Semantics note (frame indexing): a BTOR2 "frame i" reads state_i and input_i
to produce bad_i (property at that frame) AND state_{i+1} (via `next`) in
the SAME combinational pass -- this is standard BMC unrolling. "Safety up to
depth k" therefore means bad_0 .. bad_k all false, which needs input vectors
in_0 .. in_k (k transitions + one extra frame's worth of inputs to evaluate
bad_k itself, since nothing after frame k is computed). This matches
btormc's own trace convention (verified empirically: an n-transition
witness trace prints exactly n+1 input frames, @0..@n).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


class Btor2UnsupportedError(Exception):
    pass


_UNARY = {"not": "~~~", "neg": "-"}
_BINARY_BW = {"and": "&&&", "or": "|||", "xor": "^^^",
              "add": "+", "sub": "-", "mul": "*", "udiv": "/", "urem": "%"}
_CMP = {"eq", "neq", "ult", "ugte", "slt"}
_EXT = {"sext", "uext"}


@dataclass
class Btor2Program:
    sorts: dict            # sort-id -> width
    states: list            # [(id, width, has_init)]
    inputs: list            # [(id, width)]
    init_map: dict           # state_id -> (width, int value)  (resolved constant)
    next_map: dict           # state_id -> value-node-id
    bad_ids: list            # [node-id, ...]
    comb: list               # [(id, width, op, argids-or-literal), ...] ascending id
    widths: dict = field(default_factory=dict)  # any node-id -> width

    def val_width(self, nid: int) -> int:
        return self.widths[nid]


def _parse_binary_or_dec(op: str, sort_w: int, tok: str) -> int:
    if op == "const":
        if len(tok) != sort_w or any(c not in "01" for c in tok):
            raise Btor2UnsupportedError(f"const token '{tok}' doesn't match width {sort_w}")
        return int(tok, 2)
    if op == "constd":
        v = int(tok)
        if v < 0:
            v += 1 << sort_w
        return v
    raise Btor2UnsupportedError(f"unsupported const-family op '{op}'")


def parse_btor2(text: str) -> Btor2Program:
    sorts: dict[int, int] = {}
    states: list[tuple[int, int, bool]] = []
    inputs: list[tuple[int, int]] = []
    init_map: dict[int, tuple[int, int]] = {}
    next_map: dict[int, int] = {}
    bad_ids: list[int] = []
    comb: list[tuple[int, int, str, object]] = []
    widths: dict[int, int] = {}
    state_ids_no_init: set[int] = set()
    state_ids_with_init: set[int] = set()

    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        nid = int(toks[0])
        op = toks[1]
        if op == "sort":
            if toks[2] != "bitvec":
                raise Btor2UnsupportedError(f"unsupported sort kind '{toks[2]}' (line {nid})")
            sorts[nid] = int(toks[3])
            continue
        if any(t.startswith("-") for t in toks[2:] if t.lstrip("-").isdigit()):
            raise Btor2UnsupportedError(f"negative (NOT-shorthand) literal reference at line {nid}")

        if op == "input":
            w = sorts[int(toks[2])]
            inputs.append((nid, w))
            widths[nid] = w
        elif op == "state":
            w = sorts[int(toks[2])]
            widths[nid] = w
            state_ids_no_init.add(nid)  # provisional; corrected if an `init` shows up
        elif op == "init":
            sid = int(toks[3]); val_id = int(toks[4])
            state_ids_no_init.discard(sid); state_ids_with_init.add(sid)
            init_map[sid] = val_id  # resolved to a constant value in a second pass
        elif op == "next":
            sid = int(toks[3]); val_id = int(toks[4])
            next_map[sid] = val_id
        elif op == "bad":
            bad_ids.append(int(toks[2]))
        elif op in ("const", "constd"):
            w = sorts[int(toks[2])]
            v = _parse_binary_or_dec(op, w, toks[3])
            widths[nid] = w
            comb.append((nid, w, "literal", v))
        elif op in _UNARY:
            w = sorts[int(toks[2])]
            widths[nid] = w
            comb.append((nid, w, op, (int(toks[3]),)))
        elif op in _BINARY_BW or op in _CMP:
            w = sorts[int(toks[2])]
            widths[nid] = w
            comb.append((nid, w, op, (int(toks[3]), int(toks[4]))))
        elif op in _EXT:
            w = sorts[int(toks[2])]
            widths[nid] = w
            comb.append((nid, w, op, (int(toks[3]), int(toks[4]))))
        elif op == "ite":
            w = sorts[int(toks[2])]
            widths[nid] = w
            comb.append((nid, w, "ite", (int(toks[3]), int(toks[4]), int(toks[5]))))
        else:
            raise Btor2UnsupportedError(f"unsupported op '{op}' at line {nid}")

    # states list, in file order, tagged has_init
    states = []
    for nid, w in sorted(widths.items()):
        pass  # widths dict isn't ordered by declaration reliably across dict merges; rebuild below

    # Rebuild states in original declaration order by re-scanning (cheap, file is tiny).
    states = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        if toks[1] == "state":
            nid = int(toks[0])
            states.append((nid, widths[nid], nid in state_ids_with_init))

    # Resolve each init's value-id to a concrete literal (must be a const/constd node,
    # possibly transitively through other consts is NOT supported -- matches every
    # instance this generator targets, where init always points straight at a const).
    resolved_init: dict[int, tuple[int, int]] = {}
    comb_by_id = {c[0]: c for c in comb}
    for sid, val_id in init_map.items():
        node = comb_by_id.get(val_id)
        if node is None or node[2] != "literal":
            raise Btor2UnsupportedError(
                f"init of state {sid} points at non-constant node {val_id}; "
                "only literal-valued inits are supported by this generator")
        resolved_init[sid] = (node[1], node[3])

    return Btor2Program(sorts=sorts, states=states, inputs=inputs,
                         init_map=resolved_init, next_map=next_map,
                         bad_ids=bad_ids, comb=comb, widths=widths)


def unrolled_node_count(prog: Btor2Program, k: int) -> int:
    """Cheap proxy for 'unrolled gate count' (spec-week2.md WS4(d) sub-frontier
    check, '<=10^4 to start'): (#combinational nodes) * (#frames checked)."""
    return len(prog.comb) * (k + 1)


def _lit(width: int, value: int) -> str:
    return f"{value}#{width}"


def render_unrolled(prog: Btor2Program, k: int, s0_prefix="s0", frame_prefix="i") -> dict:
    """Builds the flat (no struct/step-function indirection) Lean term for
    checking frames 0..k inclusive. Returns:
      {"free_decls": [(name, width), ...] in declaration order (s0_* first,
                       then i{frame}_{id} per frame),
       "let_lines": [str, ...] (one `let n{frame}_{id} := ...` per comb node
                     per frame, in emission order),
       "bad_names": [str, ...] (length k+1, one per checked frame)}
    """
    free_decls: list[tuple[str, int]] = []
    let_lines: list[str] = []
    bad_names: list[str] = []

    # frame-0 state values: literal for init'd states, a fresh free var otherwise.
    state_vals: dict[int, str] = {}
    for sid, w, has_init in prog.states:
        if has_init:
            iw, iv = prog.init_map[sid]
            state_vals[sid] = _lit(iw, iv)
        else:
            name = f"{s0_prefix}_{sid}"
            free_decls.append((name, w))
            state_vals[sid] = name

    for frame in range(k + 1):
        input_names: dict[int, str] = {}
        for iid, w in prog.inputs:
            name = f"{frame_prefix}{frame}_{iid}"
            free_decls.append((name, w))
            input_names[iid] = name

        node_val: dict[int, str] = {}

        def val_of(nid: int) -> str:
            if nid in node_val:
                return node_val[nid]
            if nid in input_names:
                return input_names[nid]
            if nid in state_vals:
                return state_vals[nid]
            raise Btor2UnsupportedError(f"unresolved reference to node {nid} in frame {frame}")

        for nid, w, op, args in prog.comb:
            local = f"n{frame}_{nid}"
            if op == "literal":
                expr = _lit(w, args)
            elif op in _UNARY:
                expr = f"({_UNARY[op]}{val_of(args[0])})"
            elif op in _BINARY_BW:
                expr = f"({val_of(args[0])} {_BINARY_BW[op]} {val_of(args[1])})"
            elif op == "eq":
                expr = f"(if {val_of(args[0])} == {val_of(args[1])} then (1#1) else (0#1))"
            elif op == "neq":
                expr = f"(if {val_of(args[0])} == {val_of(args[1])} then (0#1) else (1#1))"
            elif op == "ult":
                expr = f"(if BitVec.ult {val_of(args[0])} {val_of(args[1])} then (1#1) else (0#1))"
            elif op == "ugte":
                expr = f"(if BitVec.ult {val_of(args[0])} {val_of(args[1])} then (0#1) else (1#1))"
            elif op == "slt":
                expr = f"(if BitVec.slt {val_of(args[0])} {val_of(args[1])} then (1#1) else (0#1))"
            elif op == "sext":
                expr = f"(BitVec.signExtend {w} {val_of(args[0])})"
            elif op == "uext":
                expr = f"(BitVec.zeroExtend {w} {val_of(args[0])})"
            elif op == "ite":
                expr = (f"(if {val_of(args[0])} == (1#1) then {val_of(args[1])} "
                         f"else {val_of(args[2])})")
            else:
                raise Btor2UnsupportedError(f"codegen: unhandled op '{op}'")
            let_lines.append(f"    let {local} : BitVec {w} := {expr}")
            node_val[nid] = local

        bad_terms = [val_of(b) for b in prog.bad_ids]
        if len(bad_terms) == 1:
            bad_expr = bad_terms[0]
        else:
            bad_expr = "(" + " ||| ".join(bad_terms) + ")"
        bad_name = f"bad{frame}"
        let_lines.append(f"    let {bad_name} : BitVec 1 := {bad_expr}")
        bad_names.append(bad_name)

        # advance state for next frame
        state_vals = {sid: val_of(prog.next_map[sid]) for sid, _, _ in prog.states}

    return {"free_decls": free_decls, "let_lines": let_lines, "bad_names": bad_names}
