"""Strict reader for locked Yosys FunctionalIR's SMT serialization, not an SMT solver.

Yosys performs RTL elaboration and normalization. This module imports its pure,
ordered transfer function. Initial assertions in the SMT file are deliberately
not used: the upstream serializer prints unknown initial bits as zero.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from lean_records import access_paths, balanced_and, record_expression, record_structures
from functional_nodes import PackedNodes
from functional_stream import bindings as streamed_bindings, transfer as streamed_transfer


@dataclass(slots=True)
class SExpr:
    value: str | list
    line: int
    comment: str = ""


def parse(text):
    roots, stack, last = [], [], None
    line, pos = 1, 0
    while pos < len(text):
        c = text[pos]
        if c.isspace():
            line += c == "\n"
            pos += 1
        elif c == ";":
            end = text.find("\n", pos)
            if end < 0:
                end = len(text)
            if last is not None:
                last.comment = text[pos + 1:end].strip()
            pos = end
        elif c == "(":
            node = SExpr([], line)
            (stack[-1].value if stack else roots).append(node)
            stack.append(node)
            pos += 1
        elif c == ")":
            if not stack:
                raise ValueError(f"SMT line {line}: unmatched closing parenthesis")
            last = stack.pop()
            pos += 1
        else:
            start = pos
            if c in {'|', '"'}:
                raise ValueError(f"SMT line {line}: quoted atoms are outside the locked serializer contract")
            while pos < len(text) and not text[pos].isspace() and text[pos] not in "();":
                pos += 1
            last = SExpr(text[start:pos], line)
            (stack[-1].value if stack else roots).append(last)
    if stack:
        raise ValueError("SMT: unclosed expression")
    return roots


def atom(e):
    if not isinstance(e.value, str):
        raise ValueError(f"SMT line {e.line}: expected atom")
    return e.value


def items(e):
    if not isinstance(e.value, list):
        raise ValueError(f"SMT line {e.line}: expected list")
    return e.value


def sort(e):
    xs = items(e)
    if len(xs) == 3 and all(isinstance(x.value, str) for x in xs[:2]) and [atom(x) for x in xs[:2]] == ["_", "BitVec"]:
        width = int(atom(xs[2]))
        if width <= 0:
            raise ValueError("FunctionalIR bit widths must be positive")
        return {"kind": "bits", "width": width}
    if len(xs) == 3 and atom(xs[0]) == "Array":
        a, b = sort(xs[1]), sort(xs[2])
        if a["kind"] != "bits" or b["kind"] != "bits":
            raise ValueError("Only bitvector-addressed bitvector memories are supported")
        return {"kind": "memory", "address_width": a["width"], "width": b["width"]}
    raise ValueError(f"SMT line {e.line}: unsupported sort")


def number(value):
    return value if isinstance(value, int) else int(value, 2)


def known_bits(text):
    if not isinstance(text, str) or any(c not in "01xz" for c in text):
        raise ValueError("Malformed Yosys initial bit string")
    return {"mask": sum(1 << i for i, c in enumerate(reversed(text)) if c in "01"),
            "value": sum(1 << i for i, c in enumerate(reversed(text)) if c == "1")}


def initial_bit_constraints(module):
    constraints = {}
    for wire in module["netnames"].values():
        if "init" in wire["attributes"]:
            for bit, value in zip(wire["bits"], reversed(wire["attributes"]["init"])):
                if value in "01":
                    if bit in constraints and constraints[bit] != value:
                        raise ValueError("Conflicting initial state constraints")
                    constraints[bit] = value
    return constraints


def initial_cell_metadata(cell):
    if cell['type'] == '$ff':
        return {'type': '$ff', 'connections': {'Q': cell['connections']['Q']}}
    if cell['type'] == '$mem_v2':
        return {'type': '$mem_v2', 'parameters': {key: cell['parameters'][key] for key in ('WIDTH', 'SIZE', 'OFFSET', 'INIT')}}
    raise ValueError('Only global FF and collected memory cells have initial-state metadata')


def import_metadata(module):
    """Retain only data used to map FunctionalIR ports and initial states."""
    return {'ports': module['ports'],
            'cells': {name: initial_cell_metadata(cell) for name, cell in module['cells'].items() if cell['type'] in {'$ff', '$mem_v2'}},
            'netnames': {name: {'bits': wire['bits'], 'attributes': {'init': wire['attributes']['init']}}
                         for name, wire in module['netnames'].items() if 'init' in wire['attributes']}}


def expose_arbitrary(module, everted=False):
    arbitrary = []
    for cell_name in [name for name, cell in module['cells'].items() if cell['type'] == '$anyseq']:
        cell = module['cells'][cell_name]
        name = cell_name + '_Y' if everted else 'c2l_undefined_' + str(len(arbitrary))
        if name in module['ports'] or name in module['netnames']:
            raise ValueError('Reserved arbitrary-input name collides with RTL: ' + name)
        bits = cell['connections']['Y']
        module['ports'][name] = {'direction': 'input', 'bits': bits}
        module['netnames'][name] = {'hide_name': 0, 'bits': bits, 'attributes': cell['attributes']}
        del module['cells'][cell_name]
        arbitrary.append({'name': name, 'width': len(bits), 'source': cell['attributes'].get('src', cell_name)})
    return arbitrary


def state_initial(module, name, typ, constraints=None):
    cell = module["cells"].get(name)
    if not cell:
        raise ValueError("FunctionalIR state cannot be matched to Yosys cell: " + name)
    if typ["kind"] == "memory":
        if cell["type"] != "$mem_v2":
            raise ValueError("Expected collected memory state")
        p = cell["parameters"]
        width, size, offset = number(p["WIDTH"]), number(p["SIZE"]), number(p["OFFSET"])
        if width != typ['width'] or size + offset > 1 << typ['address_width']:
            raise ValueError('FunctionalIR memory sort differs from the original memory: ' + name)
        bits = p["INIT"]
        values = [dict(mask=0, value=0) for _ in range(1 << typ["address_width"])]
        for address in range(size):
            chunk = bits[len(bits) - (address + 1) * width:len(bits) - address * width]
            values[(offset + address) % len(values)] = known_bits(chunk)
        return {"words": values}
    if cell["type"] != "$ff":
        raise ValueError("Expected global-clock FF after official clk2fflogic")
    if len(cell['connections']['Q']) != typ['width']:
        raise ValueError('FunctionalIR state width differs from the original FF: ' + name)
    if constraints is None:
        constraints = initial_bit_constraints(module)
    return known_bits("".join(constraints.get(bit, "x") for bit in reversed(cell["connections"]["Q"])))


def preflight(module, clock, preserve_effects=False, clock_mode="single"):
    """Check clocks/effects before clk2fflogic can obscure their original identity."""
    ports = module["ports"]
    clock_bits = None if clock is None else ports.get(clock, {}).get("bits")
    if clock is not None and (clock_bits is None or len(clock_bits) != 1 or ports[clock]["direction"] != "input"):
        raise ValueError("Clock must name a one-bit top-level input")
    regs = memories = 0
    clocked_effects = False
    global_sequential = {"$dff", "$dffe", "$sdff", "$sdffe", "$sdffce", "$adff", "$adffe",
                         "$aldff", "$aldffe", "$dffsr", "$dffsre", "$dlatch", "$adlatch", "$dlatchsr"}
    for name, cell in module["cells"].items():
        t, p, c = cell["type"], cell["parameters"], cell["connections"]
        location = cell["attributes"].get("src", name)
        def fail(message):
            raise ValueError(f"{location}: {message} ({t})")
        if t == "$scopeinfo":
            continue
        if t in {"$assert", "$assume", "$cover", "$check", "$print"} and preserve_effects:
            if t == "$check" and p.get("FLAVOR") not in {"assert", "assume", "cover"}:
                fail("Unsupported verification flavor")
            if number(p.get("TRG_ENABLE", 0)):
                clocked_effects = True
                if clock_mode == "single" and (clock_bits is None or c["TRG"] != clock_bits or number(p["TRG_POLARITY"]) != 1):
                    fail("Only a direct positive-edge effect trigger is currently supported")
            continue
        if t in {"$assert", "$assume", "$cover", "$check", "$print", "$anyseq", "$anyconst", "$allseq", "$allconst"}:
            fail("Verification, simulation effects and arbitrary-value cells need a separate contract")
        if clock_mode == "global" and t in global_sequential:
            regs += 1
            continue
        if t in {"$dff", "$dffe", "$sdff", "$sdffe", "$sdffce"}:
            regs += 1
            if clock_bits is None or c["CLK"] != clock_bits or number(p["CLK_POLARITY"]) != 1:
                fail("Only one direct, positive-edge clock is supported")
        elif t == "$mem_v2":
            memories += 1
            if number(p["OFFSET"]) != 0 or number(p["SIZE"]) < 2:
                fail("The current memory importer requires a zero-based memory with at least two words")
            for direction in ("RD", "WR"):
                enables = p[direction + "_CLK_ENABLE"]
                polarities = p[direction + "_CLK_POLARITY"]
                clocks = c[direction + "_CLK"]
                for i, enabled in enumerate(reversed(enables)):
                    if direction == "WR" and enabled != "1":
                        fail("Asynchronous memory writes require a separate timing contract")
                    if clock_mode == "single" and enabled == "1" and (clock_bits is None or [clocks[i]] != clock_bits or polarities[-1-i] != "1"):
                        fail("Memory port uses a different, gated, or negative-edge clock")
        elif re.search(r"(dff|latch|ff|sr)", t, re.I):
            fail("Asynchronous reset, latches and unrecognized sequential cells are unsupported")
        elif not t.startswith("$"):
            fail("Unresolved external module")
        for port, bits in c.items():
            # Unused asynchronous memory clocks and read reset values are irrelevant.
            if t == "$mem_v2" and port in {"RD_CLK", "WR_CLK"}:
                continue
            # Data X/Z values are exposed as caller-supplied arbitrary inputs
            # by official setundef -anyseq. Clocks must remain fully identified.
    result = {"register_cells": regs, "memory_cells": memories, "clock": clock if regs or memories or clocked_effects else None}
    if clock_mode == "global":
        result.update(clock=None, clock_mode="global")
    return result


def import_functional(text, module, namespace, clock, *, _roots=None, _bindings=None, _compact_nodes=False):
    roots = parse(text) if _roots is None else _roots
    definitions = [x for x in roots if atom(items(x)[0]) == "define-fun"]
    if len(definitions) != 1:
        raise ValueError("Expected exactly one selected FunctionalIR transfer function")
    definition = items(definitions[0])
    if len(definition) != 5:
        raise ValueError("Malformed transfer function")
    types = {}
    for root in roots:
        xs = items(root)
        if atom(xs[0]) == "declare-datatype":
            constructors = items(xs[2])
            if len(constructors) != 1:
                raise ValueError("Expected a record datatype")
            constructor = items(constructors[0])
            if atom(constructor[0]) != atom(xs[1]):
                raise ValueError("Unexpected constructor name")
            types[atom(xs[1])] = [(atom(items(f)[0]), sort(items(f)[1])) for f in constructor[1:]]
    parameters = items(definition[2])
    result = items(definition[3])
    if len(parameters) != 2 or atom(result[0]) != "Pair":
        raise ValueError("Expected (inputs, state) -> Pair outputs state")
    input_type, state_type, output_type = atom(items(parameters[0])[1]), atom(items(parameters[1])[1]), atom(result[1])
    if atom(result[2]) != state_type or [atom(items(p)[0]) for p in parameters] != ["inputs", "state"]:
        raise ValueError("Unexpected FunctionalIR function signature")
    body, bindings = definition[4], [] if _bindings is None else _bindings
    while isinstance(body.value, list) and atom(items(body)[0]) == "let":
        if _bindings is not None:
            raise ValueError('Streamed transfer header unexpectedly contains bindings')
        xs = items(body)
        group = items(xs[1])
        if len(group) != 1:
            raise ValueError("Expected the upstream serializer's single-binding let")
        b = items(group[0]); bindings.append((atom(b[0]), b[1])); body = xs[2]
    pair = items(body)
    if len(pair) != 3 or atom(pair[0]) != "pair":
        raise ValueError("Expected outputs / next state pair")
    fields, accessors, constructors = {}, {}, {}
    constraints = initial_bit_constraints(module)
    input_accessors = {input_type + '_' + name: name for name, port in module['ports'].items()
                       if port['direction'] == 'input'}
    for kind, type_name, value, prefix in (("inputs", input_type, None, "i"), ("outputs", output_type, pair[1], "o"),
                                           ("states", state_type, pair[2], "s")):
        declarations = types[type_name]
        values = [] if value is None else (items(value)[1:] if isinstance(value.value, list) else [])
        if value is not None and (len(values) != len(declarations) or (atom(items(value)[0]) if isinstance(value.value,list) else atom(value)) != type_name):
            raise ValueError("Record return arity mismatch")
        current = []
        for i, (accessor, typ) in enumerate(declarations):
            if value is None:
                if accessor not in input_accessors:
                    raise ValueError("Cannot map input accessor to original port: " + accessor)
                name = input_accessors[accessor]
            else:
                name = values[i].comment
                if not name:
                    raise ValueError("Missing original field name in locked FunctionalIR serializer")
            field = {"name": name, "field": prefix + str(i), "sort": typ}
            if kind == "states":
                field["initial"] = state_initial(module, name, typ, constraints)
            if kind == "outputs" and (name not in module["ports"] or len(module["ports"][name]["bits"]) != typ["width"]):
                raise ValueError("Output mapping differs from Yosys ports")
            current.append(field)
            accessors[accessor] = (kind, field)
        fields[kind] = current
        constructors[kind] = values
    if {f["name"] for f in fields["inputs"]} != {n for n,p in module["ports"].items() if p["direction"] == "input"}:
        raise ValueError("Input mapping is incomplete")
    if {f['name'] for f in fields['outputs']} != {n for n, p in module['ports'].items() if p['direction'] == 'output'}:
        raise ValueError('Output mapping is incomplete')
    nodes, environment = PackedNodes() if _compact_nodes else [], {}
    def node(op, typ, args=(), **extra):
        n = {"id": len(nodes), "op": op, "sort": typ, "args": list(args), **extra}
        nodes.append(n)
        return n["id"]
    def expr(e):
        if isinstance(e.value, str):
            a = e.value
            if a in environment:
                return environment[a]
            if a.startswith(("#b", "#x")):
                base, factor = (2,1) if a.startswith("#b") else (16,4)
                return node("constant", {"kind":"bits","width":(len(a)-2)*factor}, value=int(a[2:],base))
            if a in {"true","false"}:
                return node("constant", {"kind":"bool"}, value=a=="true")
            raise ValueError(f"SMT line {e.line}: unbound atom {a}")
        xs = items(e)
        if isinstance(xs[0].value, list):
            head = [atom(x) for x in items(xs[0])]
            if len(xs) != 2 or head[0] != "_":
                raise ValueError("Invalid indexed operator")
            arg = expr(xs[1]); typ = nodes[arg]["sort"]
            if typ["kind"] != "bits":
                raise ValueError("Indexed operation requires bitvector")
            op = head[1]
            if op == "extract":
                hi, lo = map(int,head[2:])
                if lo < 0 or hi < lo or hi >= typ["width"]:
                    raise ValueError("Invalid extract bounds")
                return node(op,{"kind":"bits","width":hi-lo+1},[arg],offset=lo)
            if op in {"zero_extend","sign_extend"} and len(head)==3:
                amount=int(head[2])
                if amount<0: raise ValueError("Negative extension")
                return node(op,{"kind":"bits","width":typ["width"]+amount},[arg])
            raise ValueError(f"SMT line {e.line}: unsupported indexed operation {op}")
        op = atom(xs[0])
        if op in accessors:
            kind, f = accessors[op]
            expected = "inputs" if kind=="inputs" else "state"
            if len(xs)!=2 or atom(xs[1])!=expected or kind=="outputs":
                raise ValueError("Incorrect record accessor")
            return node("input" if kind=="inputs" else "state",f["sort"],field=f["field"])
        args = [expr(x) for x in xs[1:]]
        ts = [nodes[a]["sort"] for a in args]
        def same(n, kind="bits"):
            if len(args)!=n or not ts or any(t!=ts[0] for t in ts) or ts[0]["kind"]!=kind:
                raise ValueError(f"SMT line {e.line}: sort/arity mismatch for {op}")
        if op=="ite":
            if len(args)!=3 or ts[0]!={"kind":"bool"} or ts[1]!=ts[2]: raise ValueError("Invalid ite")
            typ=ts[1]
        elif op=="concat":
            if len(args)!=2 or any(t["kind"]!="bits" for t in ts): raise ValueError("Invalid concat")
            typ={"kind":"bits","width":sum(t["width"] for t in ts)}
        elif op in {"bvadd","bvsub","bvmul","bvudiv","bvurem","bvand","bvor","bvshl","bvlshr","bvashr"}:
            same(2); typ=ts[0]
        elif op=="bvxor":
            if not args or any(t!=ts[0] for t in ts) or ts[0]["kind"]!="bits": raise ValueError("Invalid xor")
            typ=ts[0]
        elif op in {"bvnot","bvneg"}:
            same(1); typ=ts[0]
        elif op in {"=","distinct","bvugt","bvuge","bvsgt","bvsge"}:
            same(2); typ={"kind":"bool"}
        elif op=="select":
            if len(args)!=2 or ts[0]["kind"]!="memory" or ts[1]!={"kind":"bits","width":ts[0]["address_width"]}: raise ValueError("Invalid select")
            typ={"kind":"bits","width":ts[0]["width"]}
        elif op=="store":
            if len(args)!=3 or ts[0]["kind"]!="memory" or ts[1]!={"kind":"bits","width":ts[0]["address_width"]} or ts[2]!={"kind":"bits","width":ts[0]["width"]}: raise ValueError("Invalid store")
            typ=ts[0]
        else:
            raise ValueError(f"SMT line {e.line}: unsupported FunctionalIR operation {op}")
        return node(op,typ,args,line=e.line)
    for name,value in bindings:
        if name in environment: raise ValueError("Duplicate FunctionalIR binding")
        environment[name]=expr(value)
    for kind in ("outputs","states"):
        for f,value in zip(fields[kind],constructors[kind]):
            f["node"]=expr(value)
            if nodes[f["node"]]["sort"]!=f["sort"]: raise ValueError("Return field has wrong sort")
    return {"format":"chisel2lean-functional-v1","namespace":namespace,"clock":clock,"symbolic_parameters":False,
            **fields,"nodes":nodes,"initialization":"Known bits from Yosys JSON; unknown bits remain caller-supplied. SMT initial assertions discarded."}


def import_functional_file(path, module, namespace, clock, compact_nodes=False):
    snapshot = path.stat()
    roots = streamed_transfer(path)
    result = import_functional(None, module, namespace, clock, _roots=roots, _bindings=streamed_bindings(path),
                               _compact_nodes=compact_nodes)
    after = path.stat()
    if (snapshot.st_size, snapshot.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('FunctionalIR serialization changed between passes')
    return result


def lean_type(typ):
    if typ["kind"]=="bool": return "Bool"
    if typ["kind"]=="bits": return f'BitVec {typ["width"]}'
    return f'(BitVec {typ["address_width"]} → BitVec {typ["width"]})'


def emit_lean(ir, chunk_size=None, bounded_shifts=True):
    ns=ir["namespace"]
    if not re.fullmatch(r"[A-Z][A-Za-z0-9_]*(\.[A-Z][A-Za-z0-9_]*)*",ns):
        raise ValueError("Namespace must be dot-separated uppercase-leading identifiers")
    lines=["-- Generated from locked Yosys FunctionalIR. Fixed circuit; explicit initial state.",
           "import Lean","set_option linter.unusedVariables false","set_option maxRecDepth 65536","set_option maxHeartbeats 0",f"namespace {ns}"]
    group_size = ir.get('lean_record_group_size', 128)
    paths = {kind: access_paths((f['field'] for f in ir[kind]), group_size) for kind in ('inputs', 'states', 'outputs')}
    for typ,kind in (("Inputs","inputs"),("State","states"),("Outputs","outputs")):
        lines += record_structures(typ, [(f['field'], lean_type(f['sort'])) for f in ir[kind]], group_size)
    lines += ["structure Evaluation where","  outputs : Outputs","  nextState : State",
              "  outputDefined : Bool","  transitionDefined : Bool","",
              "def evaluate (inputs : Inputs) (state : State) : Evaluation :="]
    if ir.get("effects"):
        lines.insert(lines.index("  transitionDefined : Bool") + 1, "  checksValid : Bool")
    domains=[]
    for n in ir["nodes"]:
        op=n["op"]; a=[f"n{i}" for i in n["args"]]; w=n["sort"].get("width")
        if op=="constant": value=str(n["value"]).lower() if w is None else f'({n["value"]}#{w})'
        elif op in {"input","state"}:
            kind = 'inputs' if op == 'input' else 'states'
            value=f'{"inputs" if op=="input" else "state"}.{paths[kind][n["field"]]}'
        elif op=="ite": value=f'(if {a[0]} then {a[1]} else {a[2]})'
        elif op=="concat": value=f'({a[0]} ++ {a[1]})'
        elif op=="extract": value=f'(BitVec.extractLsb\' {n["offset"]} {w} {a[0]})'
        elif op=="zero_extend": value=f'(BitVec.setWidth {w} {a[0]})'
        elif op=="sign_extend": value=f'(BitVec.signExtend {w} {a[0]})'
        elif op in {"bvnot","bvneg"}: value=f'({"~~~" if op=="bvnot" else "-"}{a[0]})'
        elif op in {"bvudiv","bvurem"}: value=f'(BitVec.{"udiv" if op=="bvudiv" else "umod"} {a[0]} {a[1]})'
        elif op in {"bvshl","bvlshr"}:
            amount = f'(Nat.min {w} {a[1]}.toNat)' if bounded_shifts else f'{a[1]}.toNat'
            value=f'({a[0]} {"<<<" if op=="bvshl" else ">>>"} {amount})'
        elif op=="bvashr":
            amount = f'(Nat.min {w} {a[1]}.toNat)' if bounded_shifts else f'{a[1]}.toNat'
            value=f'({a[0]}.sshiftRight {amount})'
        elif op in {"bvadd","bvsub","bvmul","bvand","bvor","bvxor"}:
            symbol={"bvadd":"+","bvsub":"-","bvmul":"*","bvand":"&&&","bvor":"|||","bvxor":"^^^"}[op]
            value="("+f" {symbol} ".join(a)+")"
        elif op in {"=","distinct"}: value=f'({a[0]} {"==" if op=="=" else "!="} {a[1]})'
        elif op in {"bvugt","bvuge","bvsgt","bvsge"}:
            method="toInt" if op.startswith("bvs") else "toNat"
            symbol=">=" if op.endswith("ge") else ">"
            value=f'(decide ({a[0]}.{method} {symbol} {a[1]}.{method}))'
        elif op=="select": value=f'({a[0]} {a[1]})'
        elif op=="store": value=f'(fun address => if address == {a[1]} then {a[2]} else {a[0]} address)'
        else: raise ValueError("Missing Lean emitter: "+op)
        lines.append(f'  let n{n["id"]} : {lean_type(n["sort"])} := {value}')
        deps=[domains[i] for i in n["args"]]
        if op=="ite":
            domain=[deps[0], "true" if deps[1]==deps[2]=="true" else f'(if {a[0]} then {deps[1]} else {deps[2]})']
        else: domain=deps
        if op in {"bvudiv","bvurem"}: domain=domain+[f'({a[1]} != 0)']
        domain=" && ".join(dict.fromkeys(d for d in domain if d!="true")) or "true"
        if domain!="true":
            lines.append(f'  let d{n["id"]} : Bool := {domain}')
            domain=f'd{n["id"]}'
        domains.append(domain)
    def record(kind):
        return record_expression([(f['field'], 'n' + str(f['node'])) for f in ir[kind]], group_size)
    def defined(kind):
        values = []
        for f in ir[kind]:
            domain = domains[f["node"]]
            if domain != "true" and f.get("effect_role") in {"condition", "arguments"}:
                domain = f'(n{f["effect_enable_node"]} == 0 || {domain})'
            if domain != "true":
                values.append(domain)
        return " && ".join(dict.fromkeys(values)) or "true"
    checks = []
    output_fields = {f["name"]: f for f in ir["outputs"]}
    for e in ir.get("effects", []):
        if e["kind"] in {"assert", "assume"}:
            enable = output_fields[e["ports"]["enable"]]["node"]
            condition = output_fields[e["ports"]["condition"]]["node"]
            checks.append(f'(n{enable} == 0 || n{condition} != 0)')
    check_expression = balanced_and(checks) if len(checks) > 128 else (' && '.join(checks) or 'true')
    extra = ', checksValid := ' + check_expression if ir.get("effects") else ''
    lines += [f'  {{ outputs := {record("outputs")}, nextState := {record("states")},',
              f'    outputDefined := {defined("outputs")}, transitionDefined := {defined("states")}{extra} }}',
              "", "def step (inputs : Inputs) (state : State) : Outputs × State :=",
              "  let r := evaluate inputs state", "  (r.outputs, r.nextState)", "",
              "def initialValid (state : State) : Bool :="]
    conditions=[]
    for f in ir["states"]:
        access = 'state.' + paths['states'][f['field']]
        pairs=[(access,f["initial"])] if f["sort"]["kind"]=="bits" else [
            (f'({access} ({i}#{f["sort"]["address_width"]}))',v) for i,v in enumerate(f["initial"]["words"])]
        for access, init in pairs:
            if init["mask"]: conditions.append(f'(({access} &&& ({init["mask"]}#{f["sort"]["width"]})) == ({init["value"]}#{f["sort"]["width"]}))')
    lines += ["  "+balanced_and(conditions),f"end {ns}",""]
    source = "\n".join(lines)
    if chunk_size is None:
        chunk_size = 128 if len(ir['nodes']) > 4096 else 0
    if chunk_size:
        from lean_chunks import chunk_evaluation
        source = chunk_evaluation(ir, source, chunk_size)
    return source
