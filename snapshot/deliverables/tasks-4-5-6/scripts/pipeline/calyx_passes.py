"""Geometry and scheduling passes over structured Calyx.

These passes accept already-resized arithmetic phases. No pass inspects or
substitutes serialized program text. Primitive/source changes fail explicitly.
"""
from copy import deepcopy

from scripts.pipeline.calyx_program import Assignment, Cell, Expression, Group, Program, Reference


def assignment(cell: str, port: str, value: str, *, hole: bool = False) -> Assignment:
    return Assignment(Reference(cell, port, hole), Expression.text(value))


def rebind(program: Program, cell: str, port: str, expected: str, value: str) -> None:
    selected = [a for a in program.assignments() if a.target == Reference(cell, port)]
    if len(selected) != 1 or selected[0].value.parts != Expression.text(expected).parts:
        raise ValueError(f'expected exactly one {cell}.{port} assignment from {expected}')
    selected[0].value = Expression.text(value)


def remove_assignment(program: Program, cell: str, port: str, expected: str) -> None:
    sites = [(wire.assignments if isinstance(wire, Group) else program.wires, a)
             for wire in program.wires
             for a in (wire.assignments if isinstance(wire, Group) else [wire])
             if a.target == Reference(cell, port)]
    if len(sites) != 1 or sites[0][1].value.parts != Expression.text(expected).parts:
        raise ValueError(f'expected exactly one removable {cell}.{port} assignment')
    owner, selected = sites[0]
    owner.remove(selected)


def add_cell(program: Program, name: str, primitive: str, *parameters: int) -> None:
    if name in program.cells:
        raise ValueError(f'cell already exists: {name}')
    program.cells[name] = Cell(name, primitive, list(parameters))


def lower_layer_norm_geometry(program: Program, *, prefix: str, rows: int, hidden: int) -> None:
    """Row-major addresses and exact division by the channel count.

    Power-of-two widths use shifts. Width 768 uses 512+256 addressing and a
    pipelined divide; the quotient completes before its latch is executed.
    """
    if hidden not in (64, 128, 256, 512, 768) or rows not in (8, 20):
        raise ValueError('unsupported LayerNorm geometry')
    if hidden == 64:
        return
    width = (rows * hidden - 1).bit_length()
    shift = hidden.bit_length() - 1
    row_shift, row_pad = prefix + 'row_shift', prefix + 'row_pad'
    rebind(program, row_shift, 'right', f"{width}'d6", f"{width}'d{shift}")
    if hidden == 768:
        low, stride = prefix + 'row_stride_low_shift', prefix + 'row_stride_add'
        add_cell(program, low, 'std_lsh', width)
        add_cell(program, stride, 'std_add', width)
        rebind(program, prefix + 'flat_address', 'left', row_shift + '.out', stride + '.out')
        program.wires.extend((assignment(low, 'left', row_pad + '.out'), assignment(low, 'right', f"{width}'d8"),
                              assignment(stride, 'left', row_shift + '.out'), assignment(stride, 'right', low + '.out')))
    for name in ('mean_shift', 'variance_shift'):
        operation = prefix + name
        if hidden != 768:
            rebind(program, operation, 'right', "64'd6", f"64'd{shift}")
            continue
        cell = program.cells[operation]
        if cell.primitive != 'std_rsh' or cell.parameters != [64]:
            raise ValueError(f'LayerNorm division input changed: {operation}')
        left = prefix + ('sum_absolute.out' if name == 'mean_shift' else 'square_sum.out')
        remove_assignment(program, operation, 'left', left)
        remove_assignment(program, operation, 'right', "64'd6")
        cell.primitive = 'std_div_pipe'
        for a in program.assignments():
            a.value.parts = [Reference(operation, 'out_quotient') if part == Reference(operation, 'out') else part
                             for part in a.value.parts]
        group = prefix + ('calculate_mean_quotient' if name == 'mean_shift' else 'calculate_variance_quotient')
        setup = []
        if name == 'mean_shift':
            setup = [assignment(prefix + 'sum_negative', 'left', prefix + 'row_sum.out'),
                     assignment(prefix + 'sum_negative', 'right', "64'd0"),
                     assignment(prefix + 'sum_negate', 'left', "64'd0"),
                     assignment(prefix + 'sum_negate', 'right', prefix + 'row_sum.out'),
                     assignment(prefix + 'sum_absolute', 'cond', prefix + 'sum_negative.out'),
                     assignment(prefix + 'sum_absolute', 'tru', prefix + 'sum_negate.out'),
                     assignment(prefix + 'sum_absolute', 'fal', prefix + 'row_sum.out')]
        program.wires.append(Group(['group', group], setup + [
            assignment(operation, 'left', left), assignment(operation, 'right', f"64'd{hidden}"),
            assignment(operation, 'go', "1'd1"), assignment(group, 'done', operation + '.done', hole=True)]))
        latch = prefix + ('latch_mean' if name == 'mean_shift' else 'latch_variance')
        program.control.insert_before(latch, group)


def lower_causal_geometry(program: Program, *, rows: int, hidden: int, head_dim: int) -> None:
    """Distinct dense [row,head,key] and [row,head,lane] address spaces."""
    if rows not in (8, 20) or hidden not in (64, 128, 256, 512, 768) or head_dim != hidden // 16:
        raise ValueError('unsupported causal geometry')
    rw = rows.bit_length()
    vw = (rows * hidden - 1).bit_length()
    sw = (rows * rows * 16 - 1).bit_length()
    for name, primitive, parameters in (
        ('model_score_row', 'std_pad', (rw, sw)), ('model_score_row_shift', 'std_lsh', (sw,)),
        ('model_score_head', 'std_pad', (5, sw)), ('model_score_head_shift', 'std_lsh', (sw,)),
        ('model_score_key', 'std_pad', (rw, sw))):
        add_cell(program, name, primitive, *parameters)
    rebind(program, 'causal_slot_head_key', 'left', 'causal_head_shift2.out', 'model_score_head_shift.out')
    rebind(program, 'causal_slot_head_key', 'right', 'causal_key_pad8.out', 'model_score_key.out')
    rebind(program, 'causal_slot_address', 'left', 'causal_row_shift6.out', 'model_score_row_shift.out')
    program.wires.extend((assignment('model_score_row', 'in', 'causal_row.out'),
                          assignment('model_score_row_shift', 'left', 'model_score_row.out'),
                          assignment('model_score_row_shift', 'right', f"{sw}'d{7 if rows == 8 else 8}"),
                          assignment('model_score_head', 'in', 'causal_head.out'),
                          assignment('model_score_head_shift', 'left', 'model_score_head.out'),
                          assignment('model_score_head_shift', 'right', f"{sw}'d{3 if rows == 8 else 4}"),
                          assignment('model_score_key', 'in', 'causal_key.out')))
    if hidden != 64:
        for axis in ('row', 'key'):
            rebind(program, f'causal_{axis}_shift6', 'right', f"{vw}'d6", f"{vw}'d{hidden.bit_length() - 1}")
        rebind(program, 'causal_head_shift2', 'right', f"{vw}'d2", f"{vw}'d{head_dim.bit_length() - 1}")
        if hidden == 768:
            for axis in ('row', 'key', 'head'):
                low, stride = f'model_{axis}_low_shift', f'model_{axis}_stride'
                add_cell(program, low, 'std_lsh', vw)
                add_cell(program, stride, 'std_add', vw)
                high = f'causal_{axis}_shift' + ('2' if axis == 'head' else '6')
                program.wires.extend((assignment(low, 'left', f'causal_{axis}_pad8.out'),
                                      assignment(low, 'right', f"{vw}'d{4 if axis == 'head' else 8}"),
                                      assignment(stride, 'left', high + '.out'), assignment(stride, 'right', low + '.out')))
                target = {'row': 'causal_query_address', 'key': 'causal_key_address', 'head': 'causal_query_head_lane'}[axis]
                rebind(program, target, 'left', high + '.out', stride + '.out')
    if rows == 20:
        for axis, low_shift in (('row', 6), ('head', 2)):
            low, stride = f'model_score_{axis}_low_shift', f'model_score_{axis}_stride'
            add_cell(program, low, 'std_lsh', sw)
            add_cell(program, stride, 'std_add', sw)
            program.wires.extend((assignment(low, 'left', f'model_score_{axis}.out'),
                                  assignment(low, 'right', f"{sw}'d{low_shift}"),
                                  assignment(stride, 'left', f'model_score_{axis}_shift.out'), assignment(stride, 'right', low + '.out')))
            target = 'causal_slot_address' if axis == 'row' else 'causal_slot_head_key'
            rebind(program, target, 'left', f'model_score_{axis}_shift.out', stride + '.out')


def split_attention_denominator(program: Program) -> None:
    """Separate one register update from a possibly multi-cycle memory wait."""
    name, successor = 'causal_write_exp_accumulate_denominator', 'model_accumulate_denominator'
    groups = [w for w in program.wires if isinstance(w, Group) and w.name == name]
    if len(groups) != 1 or any(isinstance(w, Group) and w.name == successor for w in program.wires):
        raise ValueError('denominator split group input changed')
    group = groups[0]
    probability_cells = {'causal_delta_is_zero', 'causal_probability_select'}
    update_cells = {'causal_denominator_add', 'causal_denominator'}
    probability = [a for a in group.assignments if a.target.cell in probability_cells]
    update = [a for a in group.assignments if a.target.cell in update_cells]
    memory = [a for a in group.assignments if a.target.cell == 'attention_exp_q1_20']
    done = [a for a in group.assignments if a.target == Reference(name, 'done', True)]
    expected = Expression.text("(causal_denominator.done & attention_exp_q1_20.done) ? 1'd1")
    if (len(probability), len(update), len(memory), len(done), len(group.assignments)) != (5, 4, 4, 1, 14) or done[0].value.parts != expected.parts:
        raise ValueError('denominator split assignments changed')
    program.control.insert_after(name, successor)
    group.assignments = probability + memory + [assignment(name, 'done', 'attention_exp_q1_20.done', hole=True)]
    index = program.wires.index(group)
    program.wires.insert(index + 1, Group(['group', successor], deepcopy(probability + update) +
                                         [assignment(successor, 'done', 'causal_denominator.done', hole=True)]))
