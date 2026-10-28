"""Structural transformations preserve references and reject invalid geometry."""
from pathlib import Path
import importlib
import unittest
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '''import "primitives/core.futil";
component main(@go go: 1) -> (@done done: 1) {
  cells {
    @external a = seq_mem_d1(64, 4, 2);
    @external b = seq_mem_d1(64, 8, 3);
    counter = std_reg(3);
  }
  wires {
    group load {
      a.addr0 = 2'd3;
      a.content_en = 1'd1;
      counter.in = a.read_data;
      load[done] = a.done;
    }
    group write {
      b.addr0 = 3'd7;
      b.write_data = counter.out;
      b.write_en = 1'd1;
      write[done] = b.done;
    }
  }
  control { seq { load; write; } }
}
'''

class StructuredCalyxTest(unittest.TestCase):
    def program(self):
        self.assertTrue((ROOT / 'scripts/pipeline/calyx_program.py').is_file(), 'structured Calyx representation is missing')
        module = importlib.import_module('scripts.pipeline.calyx_program')
        return module.Program.parse(SOURCE)

    def test_rename_preserves_references(self):
        program = self.program()
        program.rename({'a': 'weights', 'load': 'fetch'})
        output = program.serialize()
        self.assertIn('weights = seq_mem_d1', output)
        self.assertIn('counter.in = weights.read_data;', output)
        self.assertIn('fetch[done] = weights.done;', output)
        self.assertIn('fetch;', output)
        self.assertNotIn('a.read_data', output)

    def test_memory_resize_checks_addresses(self):
        program = self.program()
        with self.assertRaisesRegex(ValueError, 'address'):
            program.resize_memory('a', 64, 2)
        self.assertEqual(program.cells['a'].parameters, [64, 4, 2])

    def test_alias_requires_compatible_geometry(self):
        program = self.program()
        program.alias_memories({'a': 'b'})
        self.assertNotIn('a', program.cells)
        self.assertEqual(program.cells['b'].parameters, [64, 8, 3])
        self.assertIn('counter.in = b.read_data;', program.serialize())
        incompatible = self.program()
        incompatible.cells['a'].parameters[0] = 32
        with self.assertRaisesRegex(ValueError, 'width'):
            incompatible.alias_memories({'a': 'b'})

    def test_context_bounds_follow_generation_steps(self):
        program = self.program()
        program.bind_reference('a', 'addr0', 'counter.out')
        self.assertIn('a.addr0 = counter.out;', program.serialize())
        with self.assertRaisesRegex(ValueError, 'missing'):
            program.bind_reference('missing', 'addr0', 'counter.out')

    def test_narrowing_adds_read_and_write_adapters(self):
        program = self.program()
        program.narrow_memory('b', 32, writable=True)
        output = program.serialize()
        self.assertEqual(program.cells['b'].parameters[0], 32)
        self.assertIn('b_narrow.in = counter.out;', output)
        self.assertIn('b.write_data = b_narrow.out;', output)
        self.assertIn('b_wide.in = b.read_data;', output)

    def test_component_output_assignment_roundtrips(self):
        self.program()
        from scripts.pipeline.calyx_program import Program
        source = 'component main() -> (result: 1) { cells {} wires { result = 1\'d1; } control {} }'
        self.assertIn("result = 1'd1;", Program.parse(source).serialize())

    def test_memory_binding_rejects_geometry_change_and_emits_explicit_primitive(self):
        program = self.program()
        bindings = {
            'a': {'primitive': 'closure_bram_memory', 'parameters': [64, 4, 2, 1, 0]},
            'b': {'primitive': 'closure_lutram_memory', 'parameters': [64, 8, 3, 0, 1]},
        }
        program.bind_memories(bindings, 'closure_memory.futil')
        output = program.serialize()
        self.assertIn('a = closure_bram_memory(64, 4, 2, 1, 0);', output)
        self.assertEqual(program.prefix[:3], ["import", '"closure_memory.futil"', ";"])
        bad = self.program()
        bindings['a']['parameters'][1] = 2
        with self.assertRaisesRegex(ValueError, 'geometry'):
            bad.bind_memories(bindings, 'closure_memory.futil')
        self.assertEqual(bad.cells['a'].primitive, 'seq_mem_d1')

    def test_continuous_memory_write_narrowing_terminates(self):
        self.program()
        script = "from scripts.pipeline.calyx_program import Program; p=Program.parse(\"component main() -> () { cells { @external b = seq_mem_d1(64, 4, 2); } wires { b.write_data = 64'd7; } control {} }\"); p.narrow_memory('b',32,writable=True); print(p.serialize())"
        try:
            result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT, capture_output=True, text=True, timeout=1)
        except subprocess.TimeoutExpired:
            self.fail('continuous narrowing did not terminate')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('b_narrow.in = 64\'d7;', result.stdout)
        self.assertEqual(result.stdout.count('b.write_data = b_narrow.out;'), 1)

    def test_counter_resize_updates_literals_and_rejects_unknown_cells(self):
        from scripts.pipeline.lower_fixed_point_model_to_calyx import _resize
        phase = (["counter = std_reg(3);", "less = std_lt(3);"], ["group set { counter.in = 3'd0; set[done] = counter.done; } comb group check { less.left = counter.out; less.right = 3'd4; }"], "set;")
        result = _resize(phase, {"counter": (5,), "less": (5,)}, {"less": 20})
        self.assertIn("counter = std_reg(5);", result[0])
        self.assertIn("less.right = 5'd20;", result[1][0])
        with self.assertRaises(ValueError):
            _resize(phase, {"missing": (5,)})
        with self.assertRaisesRegex(ValueError, "literal"):
            _resize(phase, {"less": (1,)}, {"less": 20})

    def test_control_insertion_visits_enables_only(self):
        from scripts.pipeline.calyx_program import Control
        control = Control.parse("seq { while check.out with load { seq { load; if choose.out with write { write; } else { load_again; } } } }")
        control.insert_before("load", "prepare")
        self.assertIn("with load", control.serialize())
        self.assertIn("prepare; load;", control.serialize())
        self.assertIn("load_again;", control.serialize())
        control.rename({"check": "condition", "write": "commit"})
        self.assertIn("while condition.out", control.serialize())
        self.assertIn("with commit", control.serialize())
        self.assertIn("commit;", control.serialize())

    def test_control_insertion_rejects_ambiguous_or_absent_enable(self):
        from scripts.pipeline.calyx_program import Control
        control = Control.parse("seq { load; load; }")
        before = control.serialize()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            control.insert_before("load", "prepare")
        self.assertEqual(before, control.serialize())
        with self.assertRaisesRegex(ValueError, "exactly one"):
            control.insert_before("missing", "prepare")

    def test_control_parser_rejects_unimplemented_forms(self):
        from scripts.pipeline.calyx_program import Control
        for source in ("invoke worker();", "while check.out {} trailing", "if check.out {} else"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                Control.parse(source)

    def test_insertion_preserves_order_inside_parallel_branch(self):
        from scripts.pipeline.calyx_program import Control
        control = Control.parse("par { load; independent; }")
        control.insert_before("load", "prepare")
        parallel = control.children[0]
        self.assertEqual(parallel.children[0].kind, 'seq')
        self.assertEqual([child.name for child in parallel.children[0].children], ['prepare', 'load'])
        self.assertEqual(parallel.children[1].name, 'independent')

if __name__ == '__main__':
    unittest.main()
