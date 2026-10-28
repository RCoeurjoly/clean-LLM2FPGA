"""Address equations and scheduling hazards, independent of emitted formatting."""
import unittest

from scripts.pipeline.calyx_program import Assignment, Cell, Control, Expression, Group, Program, Reference


class SchedulePassTest(unittest.TestCase):
    def test_checked_rebinding_rejects_wrong_source_without_mutation(self):
        from scripts.pipeline.calyx_passes import rebind
        program = Program.parse("component main() -> () { cells {} wires { target.left = source.out; } control {} }")
        before = program.serialize()
        with self.assertRaisesRegex(ValueError, 'expected'):
            rebind(program, 'target', 'left', 'wrong.out', 'replacement.out')
        self.assertEqual(program.serialize(), before)

    def test_denominator_split_keeps_register_update_out_of_memory_wait(self):
        from scripts.pipeline.calyx_passes import split_attention_denominator
        from scripts.pipeline import lower_fixed_point_model_to_calyx as model
        _, attention, _ = model._dependencies()
        program = model._phase_program(attention._causal_attention_phase())
        split_attention_denominator(program)
        groups = {g.header[g.header.index('group') + 1]: g for g in program.wires if isinstance(g, Group)}
        write = groups['causal_write_exp_accumulate_denominator']
        accumulate = groups['model_accumulate_denominator']
        self.assertFalse(any(a.target.cell == 'causal_denominator' for a in write.assignments))
        self.assertTrue(any(a.target == Reference('causal_denominator', 'write_en') for a in accumulate.assignments))
        enables = [node.name for node in program.control.walk() if node.kind == 'enable']
        index = enables.index('causal_write_exp_accumulate_denominator')
        self.assertEqual(enables[index + 1], 'model_accumulate_denominator')
        self.assertEqual(write.assignments[-1].value.parts, [Reference('attention_exp_q1_20', 'done')])
        with self.assertRaises(ValueError):
            split_attention_denominator(program)

    def test_causal_address_equations_cover_non_power_of_two_strides(self):
        from scripts.pipeline import lower_fixed_point_model_to_calyx as model
        _, attention, _ = model._dependencies()
        for rows in (8, 20):
            for hidden in (64, 128, 256, 512, 768):
                head_dim = hidden // 16
                p = model._phase_program(model._causal(attention, rows=rows, hidden=hidden, head_dim=head_dim))
                assignments = {a.target: a.value for a in p.assignments()}
                for row, key, head, lane in ((0, 0, 0, 0), (rows - 1, rows - 1, 15, head_dim - 1), (3, 2, 7, 1)):
                    known = {Reference('causal_row', 'out'): row, Reference('causal_key', 'out'): key,
                             Reference('causal_head', 'out'): head, Reference('causal_lane', 'out'): lane}
                    def evaluate(ref):
                        if ref in known:
                            return known[ref]
                        if ref in assignments:
                            part, = assignments[ref].parts
                            return evaluate(part) if isinstance(part, Reference) else int(part.split("'d")[1])
                        cell = p.cells[ref.cell]
                        if cell.primitive in ('std_pad', 'std_slice'):
                            return evaluate(Reference(ref.cell, 'in')) & ((1 << cell.parameters[-1]) - 1)
                        left = evaluate(Reference(ref.cell, 'left'))
                        right = evaluate(Reference(ref.cell, 'right'))
                        if cell.primitive == 'std_lsh':
                            return (left << right) & ((1 << cell.parameters[0]) - 1)
                        if cell.primitive == 'std_add':
                            return (left + right) & ((1 << cell.parameters[0]) - 1)
                        self.fail(f'unsupported address primitive {cell.primitive}')
                    with self.subTest(rows=rows, hidden=hidden, coordinates=(row, key, head, lane)):
                        self.assertEqual(evaluate(Reference('causal_query_address', 'out')), row * hidden + head * head_dim + lane)
                        self.assertEqual(evaluate(Reference('causal_key_address', 'out')), key * hidden + head * head_dim + lane)
                        self.assertEqual(evaluate(Reference('causal_slot_address', 'out')), (row * 16 + head) * rows + key)


if __name__ == '__main__':
    unittest.main()
