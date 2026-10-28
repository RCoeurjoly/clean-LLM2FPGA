"""Validation of the explicit primitive subset before native compilation.

Checks references, port directions/widths, literal fit and control targets.
This is an IR well-formedness gate, not an arithmetic equivalence proof.
"""
import re

from scripts.pipeline.calyx_program import Cell, Expression, Group, Program, Reference

_LITERAL = re.compile(r"(\d+)'([dbx])([0-9a-fA-F_]+)")
_COMPARISONS = {'std_' + name for name in ('lt', 'le', 'eq', 'neq', 'ge', 'gt', 'slt', 'sle', 'sgt', 'sge')}
_BINARY = {'std_' + name for name in ('add', 'sub', 'sadd', 'ssub', 'lsh', 'rsh', 'slsh', 'srsh', 'and', 'or', 'xor')}
_MEMORY = {'seq_mem_d1', 'closure_register_memory', 'closure_bram_memory', 'closure_lutram_memory'}


def primitive_ports(cell: Cell) -> tuple[dict[str, int], dict[str, int]]:
    p, params = cell.primitive, cell.parameters
    if any(type(value) is not int for value in params):
        raise ValueError(f'{cell.name}: noninteger primitive parameter')
    if p in _MEMORY:
        count = 3 if p == 'seq_mem_d1' else 5
        if len(params) != count or any(value < 1 for value in params[:3]):
            raise ValueError(f'{cell.name}: invalid memory geometry')
        width, depth, address = params[:3]
        if depth > 1 << address or count == 5 and (params[3] < 0 or params[4] not in (0, 1)):
            raise ValueError(f'{cell.name}: invalid memory parameters')
        return ({'addr0': address, 'write_data': width, 'write_en': 1, 'content_en': 1, 'clk': 1, 'reset': 1},
                {'read_data': width, 'done': 1})
    if p in ('std_slice', 'std_pad', 'std_signext'):
        if len(params) != 2 or min(params) < 1 or (params[1] > params[0] if p == 'std_slice' else params[0] > params[1]):
            raise ValueError(f'{cell.name}: invalid width adapter')
        return {'in': params[0]}, {'out': params[1]}
    if p == 'std_bit_slice':
        if len(params) != 4:
            raise ValueError(f'{cell.name}: invalid bit slice')
        width, start, end, out = params
        if not (0 <= start <= end < width and out == end - start + 1):
            raise ValueError(f'{cell.name}: invalid bit slice range')
        return {'in': width}, {'out': out}
    if p == 'std_cat':
        if len(params) != 3 or min(params) < 1 or params[0] + params[1] != params[2]:
            raise ValueError(f'{cell.name}: invalid concatenation widths')
        return {'left': params[0], 'right': params[1]}, {'out': params[2]}
    known = _COMPARISONS | _BINARY | {'std_reg', 'std_mux', 'std_not', 'std_mult_pipe', 'std_smult_pipe', 'std_div_pipe', 'std_sdiv_pipe'}
    if p not in known:
        raise ValueError(f'{cell.name}: unsupported primitive {p}')
    if len(params) != 1 or params[0] < 1:
        raise ValueError(f'{cell.name}: invalid primitive width')
    width = params[0]
    if p == 'std_reg':
        return {'in': width, 'write_en': 1, 'clk': 1, 'reset': 1}, {'out': width, 'done': 1}
    if p == 'std_mux':
        return {'cond': 1, 'tru': width, 'fal': width}, {'out': width}
    if p == 'std_not':
        return {'in': width}, {'out': width}
    inputs = {'left': width, 'right': width}
    if p.endswith('mult_pipe'):
        return inputs | {'go': 1, 'clk': 1, 'reset': 1}, {'out': width, 'done': 1}
    if p.endswith('div_pipe'):
        return inputs | {'go': 1, 'clk': 1, 'reset': 1}, {'out_quotient': width, 'out_remainder': width, 'done': 1}
    return inputs, {'out': 1 if p in _COMPARISONS else width}


def validate_program(program: Program) -> None:
    signatures = {name: primitive_ports(cell) for name, cell in program.cells.items()}
    groups = [w for w in program.wires if isinstance(w, Group)]
    by_name = {g.name: g for g in groups}
    if len(by_name) != len(groups) or set(by_name) & set(program.cells):
        raise ValueError('duplicate group or cell/group name collision')
    component_ports = {}
    component = program.prefix.index('component')
    arrow = program.prefix.index('->', component)
    for index, token in enumerate(program.prefix):
        if token == ':':
            width = program.prefix[index + 1]
            if not width.isdecimal() or int(width) < 1:
                raise ValueError('unsupported component port width')
            component_ports[program.prefix[index - 1]] = (int(width), index > arrow)

    def port_width(ref: Reference, *, target: bool = False) -> int:
        if not ref.cell:
            if ref.port not in component_ports:
                raise ValueError(f'undeclared component port {ref.port}')
            width, output = component_ports[ref.port]
            if output != target:
                raise ValueError('cannot drive a component input or read a component output')
            return width
        if ref.hole:
            if ref.cell not in by_name or ref.port not in ('go', 'done'):
                raise ValueError(f'undeclared group hole {ref.serialize()}')
            return 1
        if ref.cell not in signatures:
            raise ValueError(f'undeclared cell {ref.cell}')
        inputs, outputs = signatures[ref.cell]
        ports = inputs if target else outputs
        if ref.port not in ports:
            raise ValueError(f'{ref.serialize()}: cannot drive an output or read an input' if target else f'{ref.serialize()}: unknown output port')
        return ports[ref.port]

    def expression_width(expression: Expression) -> int:
        tokens = expression.parts
        cursor = 0

        def atom() -> int:
            nonlocal cursor
            if cursor >= len(tokens):
                raise ValueError('incomplete expression')
            part = tokens[cursor]
            cursor += 1
            if part == '!':
                if atom() != 1:
                    raise ValueError('guard negation width must be one')
                return 1
            if part == '(':
                width = logic()
                if cursor >= len(tokens) or tokens[cursor] != ')':
                    raise ValueError('unbalanced expression')
                cursor += 1
                return width
            if isinstance(part, Reference):
                return port_width(part)
            match = _LITERAL.fullmatch(part)
            if not match:
                raise ValueError(f'unsupported expression operand {part}')
            width, base, value = match.groups()
            width, value = int(width), int(value, {'d': 10, 'b': 2, 'x': 16}[base])
            if width < 1 or value >= 1 << width:
                raise ValueError(f'literal {part} does not fit its width')
            return width

        def logic() -> int:
            nonlocal cursor
            width = atom()
            while cursor < len(tokens) and tokens[cursor] in ('&', '|'):
                cursor += 1
                if width != 1 or atom() != 1:
                    raise ValueError('guard operands must have width one')
            return width

        width = logic()
        if cursor < len(tokens) and tokens[cursor] == '?':
            if width != 1:
                raise ValueError('assignment guard must have width one')
            cursor += 1
            width = atom()
        if cursor != len(tokens):
            raise ValueError('unsupported expression suffix')
        return width

    for a in program.assignments():
        if port_width(a.target, target=True) != expression_width(a.value):
            raise ValueError(f'{a.target.serialize()}: assignment width mismatch')
    for node in program.control.walk():
        if node.kind == 'enable':
            if node.name not in by_name:
                raise ValueError(f'undeclared enabled group {node.name}')
            if 'comb' in by_name[node.name].header:
                raise ValueError('cannot enable a combinational group')
        if node.condition is not None and expression_width(node.condition) != 1:
            raise ValueError('control condition must have width one')
        if node.comb_group is not None:
            if node.comb_group not in by_name or 'comb' not in by_name[node.comb_group].header:
                raise ValueError(f'undeclared combinational group {node.comb_group}')
