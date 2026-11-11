#!/usr/bin/env python3
"""
sv2lean.py — deterministic translation of a parsed SystemVerilog module into an
executable Lean 4 transition-system model with SYMBOLIC parameters.

Model shape (mirrors CircuitProver Phase I):
    structure Params where <params>; require : <constraints>
    structure Inputs (p : Params)   -- ports minus clocks
    structure Outputs (p : Params)
    structure State (p : Params)    -- registers
    def step   (p) (i : Inputs p) (s : State p) : State p     -- always_ff, nonblocking
    def output (p) (i : Inputs p) (s : State p) : Outputs p   -- assign network
    def run    (p) (s0 : State p) (ins : Nat → Inputs p) : Nat → State p

Semantics choices (documented, deliberate):
  * Nonblocking assigns read the PRE-step state `s`; writes thread through `st`.
  * All arithmetic is unsigned, modulo 2^width (SV `logic` without `signed`).
  * Width joints are made explicit with `.setWidth`; 1-bit signals are `Bool`.
  * Registers with no declaration initializer get NO fabricated `init`: `run`
    takes an arbitrary `s0` (uninitialized regs are undefined at power-on).
  * Only `[HI:0]` ranges; only always_ff + assign (always_comb rejected loudly).
"""

import json
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------------

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<num>(?:\d[\d_]*)?'[sS]?[bBoOdDhH]?[0-9a-fA-F_xXzZ?]+|\d[\d_]*)
  | (?P<id>[A-Za-z_$][\w$]*)
  | (?P<op><<<|>>>|<=|>=|==|!=|&&|\|\||<<|>>|[-+*/%&|^~!<>?:;,(){}\[\]=])
""", re.X | re.S)

KEYWORDS = {"if", "else", "begin", "end", "case", "endcase", "default"}


def tokenize(src: str) -> List[Tuple[str, str]]:
    toks = []
    pos = 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise ValueError(f"cannot tokenize at: {src[pos:pos+30]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        text = m.group(kind)
        if kind == "id" and text in KEYWORDS:
            kind = "kw"
        toks.append((kind, text))
    return toks


# ----------------------------------------------------------------------------
# Typed Lean terms
# ----------------------------------------------------------------------------

@dataclass
class T:
    """A Lean term with a shape: kind ∈ {'bool','bv','lit'}; width is a Lean Nat term for 'bv'."""
    code: str
    kind: str
    width: Optional[str] = None
    lit: Optional[int] = None      # for kind == 'lit' (None for `'1` all-ones)
    all_ones: bool = False
    unsized: bool = False          # unsized integer literal (32-bit in SystemVerilog)
    # SV width-context hazard: True when arithmetic mixed in an unsized literal, so SystemVerilog
    # evaluates the intermediate at 32 bits while the Lean model evaluates at WIDTH bits. Harmless
    # if the value is only truncated by an assignment; a semantic divergence if it feeds a right
    # shift, comparison, bit-select, concatenation or division. Such uses are REJECTED.
    wide: bool = False


def hazard(op: str, *ts: T):
    if any(t.wide for t in ts):
        raise ValueError(
            f"SystemVerilog width-context hazard: an unsized-literal arithmetic result feeds `{op}`; "
            f"SV evaluates it at 32 bits, the WIDTH-bit model would diverge. Use a sized literal "
            f"(e.g. 1'b1) or assign the intermediate to an explicit WIDTH-bit wire first.")


class Ctx:
    def __init__(self, params, inputs, state, outputs, wires):
        self.params = params            # [name]
        self.inputs = inputs            # name -> width term
        self.state = state
        self.outputs = outputs
        self.wires = wires              # name -> width term (let-bound, visible as bare name)

    def qualify(self, expr: str) -> str:
        for n in sorted(self.params, key=len, reverse=True):
            expr = re.sub(rf'\b{re.escape(n)}\b', f'p.{n}', expr)
        return expr

    def lookup(self, name: str) -> T:
        for table, prefix in ((self.inputs, "i."), (self.state, "s."), (self.wires, "")):
            if name in table:
                w = table[name]
                if w == "1":
                    return T(f"{prefix}{name}", "bool")
                return T(f"{prefix}{name}", "bv", w)
        if name in self.params:
            return T(f"p.{name}", "nat")
        raise ValueError(f"unknown identifier {name!r}")


def to_bool(t: T) -> str:
    if t.kind == "bool":
        return t.code
    if t.kind == "bv":
        return f"({t.code} != 0)"
    if t.kind == "lit":
        return "true" if (t.all_ones or (t.lit or 0) != 0) else "false"
    if t.kind == "nat":
        return f"({t.code} != 0)"
    raise ValueError(f"cannot coerce {t.kind} to Bool")


NAT_REL = {"==": "=", "!=": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥"}


def require_to_lean(expr: str, qualify) -> str:
    """`// require: MAX <= 2**WIDTH && MAX > 0` → `p.MAX ≤ 2 ^ p.WIDTH ∧ p.MAX > 0`."""
    e = qualify(expr)
    for a, b in (("**", " ^ "), ("<=", " ≤ "), (">=", " ≥ "), ("!=", " ≠ "), ("==", " = "),
                 ("&&", " ∧ "), ("||", " ∨ ")):
        e = e.replace(a, b)
    return re.sub(r"\s+", " ", e).strip()


def to_bv(t: T, width: str) -> str:
    if t.kind == "lit":
        return "(BitVec.allOnes _)" if t.all_ones else f"({t.lit}#({width}))"
    if t.kind == "bool":
        return f"((BitVec.ofBool {t.code}).setWidth ({width}))"
    if t.kind == "bv":
        return t.code if t.width == width else f"({t.code}).setWidth ({width})"
    if t.kind == "nat":
        return f"(BitVec.ofNat ({width}) {t.code})"
    raise ValueError(f"cannot coerce {t.kind} to BitVec")


def coerce_to(t: T, target: T) -> str:
    """Coerce term `t` to the shape of `target` (an lvalue)."""
    if target.kind == "bool":
        if t.kind == "bv":
            return f"({t.code}).getLsbD 0"
        return to_bool(t)
    return to_bv(t, target.width)


# ----------------------------------------------------------------------------
# Expression parser (precedence climbing) → typed Lean term
# ----------------------------------------------------------------------------

BINARY_PREC = [
    ("||",), ("&&",), ("|",), ("^",), ("&",),
    ("==", "!="), ("<", "<=", ">", ">="), ("<<", ">>", "<<<", ">>>"),
    ("+", "-"), ("*", "/", "%"),
]


class Parser:
    def __init__(self, toks: List[Tuple[str, str]], ctx: Ctx):
        self.toks = toks
        self.i = 0
        self.ctx = ctx

    # -- token helpers --
    def peek(self, k=0):
        return self.toks[self.i + k] if self.i + k < len(self.toks) else ("eof", "")

    def at(self, text):
        return self.peek()[1] == text

    def eat(self, text=None):
        tok = self.peek()
        if text is not None and tok[1] != text:
            raise ValueError(f"expected {text!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    # -- expressions --
    def expr(self) -> T:
        return self.ternary()

    def ternary(self) -> T:
        c = self.binary(0)
        if self.at("?"):
            self.eat("?")
            a = self.ternary()
            self.eat(":")
            b = self.ternary()
            return self.mux(c, a, b)
        return c

    def mux(self, c: T, a: T, b: T) -> T:
        hazard("?: condition", c)
        cond = to_bool(c)
        w = a.wide or b.wide
        # the result takes the shape of a hardware-typed branch; literals and parameter (Nat)
        # terms adapt to it (e.g. `(d == 0) ? MAX - 1 : d - 1` is a BitVec of d's width)
        shape = next((t for t in (a, b) if t.kind in ("bv", "bool")), None)
        if shape is None:
            if "nat" in (a.kind, b.kind):
                ac = a.code if a.kind == "nat" else str(a.lit)
                bc = b.code if b.kind == "nat" else str(b.lit)
                return T(f"(if {cond} then {ac} else {bc})", "nat")
            return T(f"(if {cond} then {a.lit} else {b.lit})", "lit", lit=None)
        if shape.kind == "bool":
            return T(f"(if {cond} then {to_bool(a)} else {to_bool(b)})", "bool")
        return T(f"(if {cond} then {to_bv(a, shape.width)} else {to_bv(b, shape.width)})", "bv", shape.width, wide=w)

    def binary(self, level: int) -> T:
        if level >= len(BINARY_PREC):
            return self.unary()
        lhs = self.binary(level + 1)
        while self.peek()[1] in BINARY_PREC[level]:
            op = self.eat()[1]
            rhs = self.binary(level + 1)
            lhs = self.binop(op, lhs, rhs)
        return lhs

    def binop(self, op: str, a: T, b: T) -> T:
        if op in ("&&", "||"):
            return T(f"({to_bool(a)} {op} {to_bool(b)})", "bool")
        # parameter (Nat) arithmetic, e.g. `MAX - 1`, `2*WIDTH`: stays a Lean Nat term
        if {a.kind, b.kind} <= {"nat", "lit"} and "nat" in (a.kind, b.kind):
            ac = a.code if a.kind == "nat" else str(a.lit)
            bc = b.code if b.kind == "nat" else str(b.lit)
            if op in NAT_REL:
                return T(f"(decide ({ac} {NAT_REL[op]} {bc}))", "bool")
            lop = {"<<": "<<<", ">>": ">>>", "<<<": "<<<", ">>>": ">>>"}.get(op, op)
            return T(f"({ac} {lop} {bc})", "nat")
        # decide the common shape
        shape = a if a.kind in ("bv", "bool") else b
        if shape.kind == "lit":
            # literal-literal: fold as Nat expression
            return T(f"({a.lit} {op} {b.lit})", "lit", lit=None)
        if op in ("==", "!="):
            hazard(op, a, b)
            if shape.kind == "bool":
                return T(f"({to_bool(a)} {op} {to_bool(b)})", "bool")
            return T(f"({to_bv(a, shape.width)} {op} {to_bv(b, shape.width)})", "bool")
        if op in ("<", "<=", ">", ">="):
            hazard(op, a, b)
            w = shape.width if shape.kind == "bv" else "1"
            x, y = to_bv(a, w), to_bv(b, w)
            fn = {"<": f"BitVec.ult {x} {y}", "<=": f"BitVec.ule {x} {y}",
                  ">": f"BitVec.ult {y} {x}", ">=": f"BitVec.ule {y} {x}"}[op]
            return T(f"({fn})", "bool")
        if shape.kind == "bool":
            lop = {"&": "&&", "|": "||", "^": "^^"}.get(op)
            if lop is None:
                raise ValueError(f"operator {op} on 1-bit operands not supported")
            return T(f"({to_bool(a)} {lop} {to_bool(b)})", "bool")
        w = shape.width
        # wide-ness: arithmetic with an unsized literal, or anything built from a wide operand
        wide = a.wide or b.wide or (op in ("+", "-", "*") and
                                    ((a.kind == "lit" and a.unsized) or (b.kind == "lit" and b.unsized)))
        if op in ("<<", "<<<", ">>", ">>>"):
            hazard("shift amount", b)
            if op in (">>", ">>>"):
                hazard(op, a)  # right shift would bring the 32-bit carry bits down into range
            amt = b.code if b.kind == "nat" else (str(b.lit) if b.kind == "lit" else f"({to_bv(b, b.width)}).toNat")
            lop = "<<<" if op in ("<<", "<<<") else ">>>"
            return T(f"({to_bv(a, w)} {lop} {amt})", "bv", w, wide=wide)
        if op in ("/", "%"):
            hazard(op, a, b)
        lop = {"+": "+", "-": "-", "*": "*", "/": "/", "%": "%",
               "&": "&&&", "|": "|||", "^": "^^^"}[op]
        return T(f"({to_bv(a, w)} {lop} {to_bv(b, w)})", "bv", w, wide=wide)

    def unary(self) -> T:
        tok = self.peek()
        if tok[1] in ("!", "~", "-"):
            self.eat()
            x = self.unary()
            if tok[1] == "!":
                hazard("!", x)
                return T(f"(!{to_bool(x)})", "bool")
            if tok[1] == "~":
                if x.kind == "bool":
                    return T(f"(!{x.code})", "bool")
                return T(f"(~~~{to_bv(x, x.width)})", "bv", x.width, wide=x.wide)
            if x.kind == "lit":
                return T(f"(-{x.lit})", "lit", lit=None, unsized=x.unsized)
            return T(f"(-{to_bv(x, x.width)})", "bv", x.width, wide=x.wide)
        return self.postfix(self.primary())

    def postfix(self, x: T) -> T:
        while self.at("["):
            hazard("bit/part-select", x)
            self.eat("[")
            hi = self.index_term()
            if self.at(":"):
                self.eat(":")
                lo = self.index_term()
                self.eat("]")
                x = T(f"(BitVec.extractLsb ({hi}) ({lo}) {to_bv(x, x.width)})", "bv", f"({hi}) - ({lo}) + 1")
            else:
                self.eat("]")
                x = T(f"({to_bv(x, x.width)}).getLsbD ({hi})", "bool")
        return x

    def index_term(self) -> str:
        """An index expression as a Lean Nat term."""
        t = self.expr()
        if t.kind == "lit":
            return str(t.lit)
        if t.kind == "nat":
            return t.code
        return f"({to_bv(t, t.width)}).toNat"

    def primary(self) -> T:
        kind, text = self.peek()
        if text == "(":
            self.eat("(")
            e = self.expr()
            self.eat(")")
            return e
        if text == "{":
            self.eat("{")
            parts = [self.expr()]
            while self.at(","):
                self.eat(",")
                parts.append(self.expr())
            self.eat("}")
            for p_ in parts:
                hazard("concatenation", p_)
                if p_.kind not in ("bv", "bool"):
                    raise ValueError("concatenation of unsized literal not supported")
            codes = [to_bv(p_, p_.width) if p_.kind == "bv" else f"(BitVec.ofBool {p_.code})" for p_ in parts]
            widths = [p_.width if p_.kind == "bv" else "1" for p_ in parts]
            return T("(" + " ++ ".join(codes) + ")", "bv", "(" + " + ".join(widths) + ")")
        if kind == "num":
            self.eat()
            return self.number(text)
        if kind == "id":
            self.eat()
            return self.ctx.lookup(text)
        raise ValueError(f"unexpected token {text!r} in expression")

    def number(self, text: str) -> T:
        text = text.replace("_", "")
        if "'" not in text:
            return T(text, "lit", lit=int(text), unsized=True)
        size, rest = text.split("'")
        rest = rest.lstrip("sS")
        base, digits = (rest[0].lower(), rest[1:]) if rest[0].lower() in "bodh" else ("d", rest)
        if not size:
            if digits in ("0", "1") and base == "d":
                # unsized fill literals '0 / '1
                return T("0", "lit", lit=0) if digits == "0" else T("allOnes", "lit", lit=None, all_ones=True)
        val = int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base])
        if size == "1":
            return T("true" if val else "false", "bool")
        return T(str(val), "lit", lit=val)


# ----------------------------------------------------------------------------
# Statement translation (always_ff body) → state-threading Lean term
# ----------------------------------------------------------------------------

class StmtTranslator(Parser):
    """Translates a nonblocking-assignment statement tree into a Lean `State` term."""

    def statement(self, cur: str) -> str:
        kind, text = self.peek()
        if text == "begin":
            self.eat("begin")
            body = cur
            n = 0
            while not self.at("end"):
                body = self.statement(body)
                n += 1
            self.eat("end")
            return body
        if text == "if":
            self.eat("if")
            self.eat("(")
            c = to_bool(self.expr())
            self.eat(")")
            then_ = self.statement(cur)
            else_ = cur
            if self.at("else"):
                self.eat("else")
                else_ = self.statement(cur)
            return f"(if {c} then\n      {then_}\n    else\n      {else_})"
        if text == "case":
            return self.case(cur)
        if text == ";":
            self.eat(";")
            return cur
        if kind == "id":
            name = self.eat()[1]
            target = self.ctx.lookup(name)
            if self.at("["):
                raise ValueError("indexed register writes not supported yet")
            op = self.eat()[1]
            if op not in ("<=", "="):
                raise ValueError(f"expected assignment after {name!r}, got {op!r}")
            rhs = self.expr()  # assignment truncates to the target width: a wide RHS is fine here
            self.eat(";")
            if name not in self.ctx.state:
                raise ValueError(f"always_ff writes non-register {name!r}")
            return f"{{ {cur} with {name} := {coerce_to(rhs, target)} }}"
        raise ValueError(f"unsupported statement starting at {text!r}")

    def case(self, cur: str) -> str:
        self.eat("case")
        self.eat("(")
        sel = self.expr()
        self.eat(")")
        arms = []
        default = cur
        while not self.at("endcase"):
            if self.at("default"):
                self.eat("default")
                self.eat(":")
                default = self.statement(cur)
                continue
            labels = [self.expr()]
            while self.at(","):
                self.eat(",")
                labels.append(self.expr())
            self.eat(":")
            body = self.statement(cur)
            arms.append((labels, body))
        self.eat("endcase")
        out = default
        for labels, body in reversed(arms):
            cond = " || ".join(f"({to_bv(sel, sel.width)} == {to_bv(l, sel.width)})" for l in labels)
            out = f"(if {cond} then {body} else {out})"
        return out


# ----------------------------------------------------------------------------
# Module → Lean file
# ----------------------------------------------------------------------------

def range_width(width_expr: str, ctx: Ctx) -> str:
    if not width_expr or width_expr.strip() == "1":  # scalar `logic x;` (parser marks it "1")
        return "1"
    m = re.fullmatch(r'\s*\[([^\]]+)\]\s*', width_expr)
    if not m:
        raise ValueError(f"unsupported width syntax: {width_expr!r}")
    hi, _, lo = m.group(1).partition(':')
    hi, lo = hi.strip(), lo.strip()
    if lo != '0':
        raise ValueError(f"only [HI:0] ranges supported, got {width_expr!r}")
    if hi.isdigit():
        return str(int(hi) + 1)
    if hi.endswith('-1'):
        return ctx.qualify(hi[:-2].strip())
    return f"({ctx.qualify(hi)} + 1)"


def lean_type(width: str) -> str:
    return "Bool" if width == "1" else f"BitVec ({width})"


ASSIGN_TARGET_RE = re.compile(r'(?:^|[;)]|\bbegin\b|\belse\b)\s*(\w+)\s*<=')


def classify(mod: Dict):
    """Split declarations into registers and wires.

    A declared `logic` is a REGISTER iff it is written with `<=` inside an always_ff block;
    otherwise it is a WIRE (driven by `assign`). Output ports written in always_ff are
    registered outputs: they become State fields and `output` just reads them back.
    Returns (regs, wires, out_regs) as lists of the original dicts.
    """
    written = set()
    for b in mod["always_blocks"]:
        if b["block_type"] == "always_ff":
            written |= set(ASSIGN_TARGET_RE.findall(b["body"]))
    regs = [v for v in mod["state_vars"] if v["name"] in written]
    wires = [v for v in mod["state_vars"] if v["name"] not in written]
    out_regs = [p for p in mod["ports"] if p["direction"] == "output" and p["name"] in written]
    return regs, wires, out_regs


def generate_lean_model(mod: Dict) -> str:
    name = mod["module"]
    params = [p["name"] for p in mod["parameters"]]
    ctx = Ctx(params, {}, {}, {}, {})

    # clocks: any signal named in a posedge/negedge trigger
    clocks = set()
    for b in mod["always_blocks"]:
        for m in re.finditer(r'(?:posedge|negedge)\s+(\w+)', b["trigger"] or ""):
            clocks.add(m.group(1))

    for port in mod["ports"]:
        w = range_width(port["width_expr"], ctx)
        if port["direction"] == "input":
            if port["name"] not in clocks:
                ctx.inputs[port["name"]] = w
        elif port["direction"] == "output":
            ctx.outputs[port["name"]] = w
        else:
            raise ValueError("inout ports not supported")

    reg_list, wire_list, out_regs = classify(mod)
    regs = {v["name"]: v for v in reg_list}
    for v in reg_list:
        ctx.state[v["name"]] = range_width(v["width_expr"], ctx)
    for port in out_regs:  # registered outputs live in State; `output` reads them back
        ctx.state[port["name"]] = ctx.outputs[port["name"]]
    declared_wires = {v["name"]: range_width(v["width_expr"], ctx) for v in wire_list}

    # --- always_ff → step ---
    ff_blocks = [b for b in mod["always_blocks"] if b["block_type"] == "always_ff"]
    if any(b["block_type"] == "always_comb" for b in mod["always_blocks"]):
        raise ValueError("always_comb not supported yet (use assign)")

    # --- assign → wires and outputs (source order); wires are typed by their declaration ---
    wire_lets, out_fields = [], {}
    for b in mod["always_blocks"]:
        if b["block_type"] != "assign":
            continue
        lhs, _, rhs = b["body"].rstrip(";").partition("=")
        lhs = lhs.strip()
        rhs_t = Parser(tokenize(rhs), ctx).expr()
        if lhs in ctx.outputs:
            out_fields[lhs] = coerce_to(rhs_t, T("", "bool") if ctx.outputs[lhs] == "1" else T("", "bv", ctx.outputs[lhs]))
        elif lhs in declared_wires:
            w = declared_wires[lhs]
            ctx.wires[lhs] = w
            wire_lets.append(f"  let {lhs} : {lean_type(w)} := "
                             f"{coerce_to(rhs_t, T('', 'bool') if w == '1' else T('', 'bv', w))}")
        else:
            raise ValueError(f"assign to undeclared or register signal {lhs!r}")
    for port in out_regs:
        out_fields[port["name"]] = f"s.{port['name']}"
    missing = set(ctx.outputs) - set(out_fields)
    if missing:
        raise ValueError(f"outputs never assigned: {sorted(missing)}")
    undriven = set(declared_wires) - set(ctx.wires)
    if undriven:
        raise ValueError(f"declared wires never assigned: {sorted(undriven)}")

    lets = ("\n".join(wire_lets) + "\n") if wire_lets else ""

    # step body is translated AFTER wires are known so always_ff may read them
    step_body = "s"
    for b in ff_blocks:
        tr = StmtTranslator(tokenize(b["body"]), ctx)
        step_body = tr.statement(step_body)
        if tr.peek()[0] != "eof":
            raise ValueError(f"trailing tokens in always_ff body: {tr.peek()}")

    # --- initializers ---
    inits = {}
    for v in reg_list:
        if v["init_value"]:
            t = Parser(tokenize(v["init_value"]), ctx).expr()
            inits[v["name"]] = coerce_to(t, T("", "bool") if ctx.state[v["name"]] == "1" else T("", "bv", ctx.state[v["name"]]))
    all_init = len(inits) == len(regs) and len(regs) > 0

    L = []
    L.append(f"-- Generated by sv2lean from {name}.sv — DO NOT EDIT (frozen model)")
    L.append(f"-- Parameters are symbolic; all arithmetic is unsigned modulo 2^width.")
    L.append(f"namespace {name}")
    L.append("set_option linter.unusedVariables false  -- uniform (p i s) signatures\n")
    L.append("structure Params where")
    for p in mod["parameters"]:
        L.append(f"  {p['name']} : Nat")
    # constraints: every parameter positive, plus `// require: ...` annotations (params unqualified
    # inside the structure, since fields are in scope)
    cons_parts = [f"{p['name']} > 0" for p in mod["parameters"]]
    cons_parts += [require_to_lean(r, lambda e: e) for r in mod.get("requires", [])]
    L.append(f"  require : {' ∧ '.join(cons_parts) or 'True'}\n")

    L.append("structure Inputs (p : Params) where")
    for n, w in ctx.inputs.items():
        L.append(f"  {n} : {lean_type(w)}")
    L.append("  deriving Repr\n")
    L.append("structure Outputs (p : Params) where")
    for n, w in ctx.outputs.items():
        L.append(f"  {n} : {lean_type(w)}")
    L.append("  deriving Repr\n")
    L.append("structure State (p : Params) where")
    for n, w in ctx.state.items():
        L.append(f"  {n} : {lean_type(w)}")
    L.append("  deriving Repr\n")

    if all_init:
        L.append("def init (p : Params) : State p :=")
        L.append("  { " + ", ".join(f"{k} := {v}" for k, v in inits.items()) + " }\n")
    else:
        L.append("-- No `init`: register(s) without declaration initializers are undefined at")
        L.append("-- power-on. State theorems must quantify over `s0` or assume a reset cycle.\n")

    L.append("/-- One clock edge: nonblocking assigns read pre-state `s`. -/")
    L.append("def step (p : Params) (i : Inputs p) (s : State p) : State p :=")
    L.append(lets + "  " + step_body + "\n")

    L.append("/-- Combinational output network. -/")
    L.append("def output (p : Params) (i : Inputs p) (s : State p) : Outputs p :=")
    L.append(lets + "  { " + ", ".join(f"{k} := {v}" for k, v in out_fields.items()) + " }\n")

    L.append("/-- Trace from an arbitrary initial state. -/")
    L.append("def run (p : Params) (s0 : State p) (ins : Nat → Inputs p) : Nat → State p")
    L.append("  | 0 => s0")
    L.append("  | t + 1 => step p (ins t) (run p s0 ins t)\n")
    L.append(f"end {name}")
    return "\n".join(L) + "\n"


def main():
    if len(sys.argv) < 2:
        print("Usage: sv2lean.py <parsed.json>", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        mod = json.load(f)
    print(generate_lean_model(mod), end="")


if __name__ == "__main__":
    main()
