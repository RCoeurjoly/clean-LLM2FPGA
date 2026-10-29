"""Native mux grouping matches the baseline pass."""
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest


class MuxGroupingTests(unittest.TestCase):
    def optimize(self, source, directory, *, baseline=False):
        label = 'baseline' if baseline else 'candidate'
        fixture = directory / 'input.il'
        fixture.write_text(source)
        output = directory / (label + '.il')
        executable = os.environ.get('LLM2FPGA_YOSYS_BASELINE', 'yosys') if baseline else os.environ.get('YOSYS', 'yosys')
        arguments = [executable, '-Q', '-T']
        name = 'opt_reduce'
        if not baseline and os.environ.get('LLM2FPGA_YOSYS_REDUCE_PLUGIN'):
            arguments += ['-m', os.environ['LLM2FPGA_YOSYS_REDUCE_PLUGIN']]
            name = 'opt_reduce_keyed'
        script = f'read_rtlil {fixture}; {name}; check -assert; rename -enumerate; write_rtlil {output}'
        result = subprocess.run(arguments + ['-p', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_bytes()

    def test_group_order_aliases_defaults_and_undefined_values_match_baseline(self):
        rng = random.Random(746)
        for width in (1, 4, 14, 64):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as temporary:
                lines = ['module \\top', f' wire width {width} input 1 \\a',
                         f' wire width {width} input 2 \\b', ' wire width 32 input 3 \\s',
                         f' wire width {width} \\alias', ' connect \\alias \\a']
                values = [f"{width}'" + digit * width for digit in ('0', '1', 'x', 'z')]
                values += ['\\a', '\\b', '\\alias']
                for index in range(64):
                    count = 1 + index % 32
                    default = values[index % len(values)]
                    data = [rng.choice(values) for _ in range(count)]
                    if index % 3 == 0:
                        data = [default] * count
                    elif index % 3 == 1:
                        data = [values[(index + 1) % len(values)]] * count
                    lines += [f' wire width {width} output {index + 4} \\y{index}',
                              f' cell $pmux \\mux{index}', f' parameter \\WIDTH {width}',
                              f' parameter \\S_WIDTH {count}', f' connect \\A {default}',
                              " connect \\B { " + ' '.join(reversed(data)) + ' }',
                              f' connect \\S \\s [{count - 1}:0]',
                              f' connect \\Y \\y{index}', ' end']
                source = '\n'.join(lines + ['end']) + '\n'
                directory = Path(temporary)
                self.assertEqual(self.optimize(source, directory, baseline=True),
                                 self.optimize(source, directory))
