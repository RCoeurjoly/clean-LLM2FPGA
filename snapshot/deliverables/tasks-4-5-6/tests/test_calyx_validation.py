import unittest

from scripts.pipeline.calyx_program import Program


class CalyxValidationTest(unittest.TestCase):
    SOURCE = """component main() -> () {
      cells { value = std_reg(8); }
      wires { group write { value.in = 8'd255; value.write_en = 1'd1; write[done] = value.done; } }
      control { write; }
    }"""

    def validate(self, program):
        from scripts.pipeline.calyx_validation import validate_program
        validate_program(program)

    def test_valid_program_and_unknown_references(self):
        program = Program.parse(self.SOURCE)
        self.validate(program)
        program.bind_reference('value', 'in', 'missing.out')
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            self.validate(program)

    def test_bad_width_and_constant_fail_before_compilation(self):
        for value in ("7'd127", "8'd256"):
            with self.subTest(value=value):
                program = Program.parse(self.SOURCE)
                program.bind_reference('value', 'in', value)
                with self.assertRaisesRegex(ValueError, 'width|literal'):
                    self.validate(program)

    def test_unknown_group_and_output_driver_are_errors(self):
        program = Program.parse(self.SOURCE)
        program.control.children[0].name = 'missing'
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            self.validate(program)
        program = Program.parse(self.SOURCE)
        from scripts.pipeline.calyx_program import Reference
        program.wires[0].assignments[0].target = Reference('value', 'out')
        with self.assertRaisesRegex(ValueError, 'output'):
            self.validate(program)

    def test_unknown_primitive_is_not_silently_assumed_valid(self):
        program = Program.parse(self.SOURCE)
        program.cells['value'].primitive = 'custom_unknown'
        with self.assertRaisesRegex(ValueError, 'unsupported primitive'):
            self.validate(program)

    def test_single_component_parser_rejects_unhandled_components(self):
        with self.assertRaisesRegex(ValueError, 'single component'):
            Program.parse(self.SOURCE + self.SOURCE)

    def test_component_input_is_a_reference_with_declared_width(self):
        from scripts.pipeline.calyx_program import Reference
        program = Program.parse("component main(input: 8) -> (result: 8) { cells {} wires { result = input; } control {} }")
        self.assertEqual(program.wires[0].value.parts, [Reference('', 'input')])
        self.validate(program)

    def test_component_input_cannot_be_driven_internally(self):
        program = Program.parse("component main(input: 8) -> () { cells {} wires { input = 8'd1; } control {} }")
        with self.assertRaisesRegex(ValueError, 'input'):
            self.validate(program)


if __name__ == '__main__':
    unittest.main()
