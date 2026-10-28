"""Native cached-bit FSM optimization preserves input-removal semantics."""
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time
import unittest

class FsmOptimizationTests(unittest.TestCase):
    def run_optimizer(self, source, output, tool):
        result = subprocess.run([tool, '-Q', '-T', '-p',
            f'read_rtlil {source}; fsm_opt; rename -enumerate; write_rtlil {output}'],
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_bytes()

    def test_1024_state_optimizer_within_guard(self):
        source = Path(__file__).parent / 'fixtures/fsm-opt/input-1024.il'
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            self.run_optimizer(source, Path(temporary)/'result.il', os.environ.get('YOSYS', 'yosys'))
            self.assertLess(time.monotonic()-started, 2.0)

    def test_alias_feedback_constant_and_unknown_inputs_match_baseline(self):
        source = (Path(__file__).parent/'fixtures/fsm-opt/input-64.il').read_text()
        rng = random.Random(1701)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case in range(40):
                bits = []
                for index in range(65):
                    choices = [f'\\advance [{index%64}]', f'\\advance [{index%4}]',
                               f'\\decoded [{index%64}]', "1'0", "1'1", "1'x", "1'z"]
                    bits.append(choices[(index+case)%len(choices)] if case<7 else rng.choice(choices))
                mutated = source.replace('connect \\CTRL_IN { \\advance \\reset }',
                                         'connect \\CTRL_IN { '+' '.join(bits)+' }')
                self.assertNotEqual(source, mutated)
                path=root/'input.il';path.write_text(mutated)
                with self.subTest(case=case):
                    baseline=self.run_optimizer(path, root/'baseline.il', os.environ['LLM2FPGA_YOSYS_BASELINE'])
                    candidate=self.run_optimizer(path, root/'candidate.il', os.environ.get('YOSYS','yosys'))
                    self.assertEqual(baseline,candidate)
