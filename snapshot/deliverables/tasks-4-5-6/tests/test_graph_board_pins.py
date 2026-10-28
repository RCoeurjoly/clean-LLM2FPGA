"""Open board metadata is parsed as input, never used to rewrite emitted HDL."""
import tempfile
import unittest
from pathlib import Path

from compiler.litedram_export import PIN_COUNTS, SCALAR_PINS


class BoardPinTests(unittest.TestCase):
    def metadata(self):
        names = {'a': 'addr', 'clk_p': 'ck_p', 'clk_n': 'ck_n'}
        rows = []
        number = 0
        for key, count in PIN_COUNTS.items():
            for index in reversed(range(count)):
                rows.append(f'NET "ddr3_{names.get(key, key)}[{index}]" LOC = "P{number}" | ;')
                number += 1
        for key in SCALAR_PINS:
            rows.append(f'NET "ddr3_{key}" LOC = "P{number}" | ;')
            number += 1
        return '\n'.join(rows)

    def parse(self, text):
        from compiler.board_pins import read_ddr_pins
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'memory.ucf'
            source.write_text(text)
            return read_ddr_pins(source)

    def test_complete_nine_lane_input_is_ordered_by_signal_index(self):
        pins = self.parse(self.metadata())
        self.assertEqual(len(pins['dq']), 72)
        self.assertEqual(len(pins['dqs_p']), 9)
        self.assertEqual(pins['a'], [f'P{index}' for index in reversed(range(15))])

    def test_missing_duplicate_extra_and_shared_pins_are_rejected(self):
        text = self.metadata()
        variants = [text.split('\n', 1)[1], text + '\n' + text.splitlines()[0],
                    text + '\nNET "ddr3_dq[72]" LOC = "Q1" | ;',
                    text.replace('"P1"', '"P0"'),
                    text + '\nNET "ddr3_dm[0]" LOC = "Q1" | ;']
        for variant in variants:
            with self.subTest(variant=variant[-80:]), self.assertRaises(ValueError):
                self.parse(variant)

    def test_comment_does_not_supply_a_missing_pin(self):
        text = self.metadata()
        with self.assertRaises(ValueError):
            self.parse('# ' + text)

    def test_unparsed_statement_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse(self.metadata() + '\nNET "ddr3_dq[0]" LOC = BAD;')

    def test_stage_preserves_raw_input_dependency_and_invalidates_stale_success(self):
        import sys
        from compiler.board_pins import export_board_pins_stage
        from compiler.stages import verify_stage_receipt
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'memory.ucf'
            source.write_text(self.metadata())
            export_board_pins_stage(source, root / 'stage', python=Path(sys.executable))
            dependencies = verify_stage_receipt(root / 'stage')
            self.assertIn(str(source.resolve()), dependencies)
            self.assertIn(str((root / 'stage/pins.json').resolve()), dependencies)
            source.write_text('')
            with self.assertRaises(Exception):
                export_board_pins_stage(source, root / 'stage', python=Path(sys.executable))
            self.assertFalse((root / 'stage/success.json').exists())

    def test_missing_input_also_removes_previous_acceptance(self):
        import sys
        from compiler.board_pins import export_board_pins_stage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'memory.ucf'
            source.write_text(self.metadata())
            export_board_pins_stage(source, root / 'stage', python=Path(sys.executable))
            source.unlink()
            with self.assertRaises(FileNotFoundError):
                export_board_pins_stage(source, root / 'stage', python=Path(sys.executable))
            self.assertFalse((root / 'stage/success.json').exists())

    def test_output_overlap_preserves_input_directory(self):
        import sys
        from compiler.board_pins import export_board_pins_stage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'memory.ucf'
            source.write_text(self.metadata())
            marker = root / 'success.json'
            marker.write_text('preserve input directory')
            with self.assertRaisesRegex(ValueError, 'overlaps'):
                export_board_pins_stage(source, root, python=Path(sys.executable))
            self.assertEqual(marker.read_text(), 'preserve input directory')
