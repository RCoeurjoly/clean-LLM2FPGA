"""Recover only the exact signed64 restoring-root tail; execute native IR."""
import math
import os
import random
import subprocess
import unittest


def fixture(*, steps=32):
    lines = ['module { func.func @root(%input: i64, %initial: i64) -> i64 {',
             '%one = arith.constant 1 : i64']
    remainder, result = '%input', '%initial'
    for i in range(steps):
        bit = 1 << (2 * (steps - 1 - i))
        lines += [f'%bit{i} = arith.constant {bit} : i64',
                  f'%candidate{i} = arith.addi {result}, %bit{i} : i64',
                  f'%take{i} = arith.cmpi sge, {remainder}, %candidate{i} : i64',
                  f'%sub{i} = arith.subi {remainder}, %candidate{i} : i64',
                  f'%half{i} = arith.shrsi {result}, %one : i64',
                  f'%plus{i} = arith.addi %half{i}, %bit{i} : i64',
                  f'%r{i} = arith.select %take{i}, %sub{i}, {remainder} : i64',
                  f'%s{i} = arith.select %take{i}, %plus{i}, %half{i} : i64']
        remainder, result = f'%r{i}', f'%s{i}'
    lines += [f'return {result} : i64', '} }']
    return '\n'.join(lines)


class RestoringSqrtTests(unittest.TestCase):
    def lower(self, text):
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        result = subprocess.run([tool, '--verify-each', '--canonicalize',
                                 '--llm2fpga-recover-restoring-sqrt-loops', '--canonicalize'],
                                input=text, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


    def test_actual_cpu_execution_matches_independent_integer_reference(self):
        self.check_cpu_execution(32)

    def test_shortened_chain_execution_preserves_arbitrary_signed_states(self):
        self.check_cpu_execution(29)

    def check_cpu_execution(self, steps):
        from test_graph_sequential_scratch import SequentialScratchTests
        rng = random.Random(0)
        values = {-(1 << 63), -1, 0, 1, 2, (1 << 63)-1}
        for exponent in range(32):
            square = ((1 << exponent)-1)**2
            values.update(v for v in (square-1, square, square+1) if 0 <= v < 1 << 63)
        values.update(rng.randrange(-(1 << 63), 1 << 63) for _ in range(64))
        main = ['func.func @main() -> i64 {', '%zero = arith.constant 0 : i64']
        accumulator = '%zero'
        # A shortened input chain saturates at its available result bits. This
        # fixture checks that existing chain, without changing model precision.
        cases = [(value, 0, min(math.isqrt(max(0, value)), (1 << steps)-1))
                 for value in sorted(values)]
        def signed64(value):
            return ((value+(1 << 63)) % (1 << 64))-(1 << 63)
        # Nonzero incoming state checks that recovery makes no unstated range
        # assumption and preserves wrapping candidate addition and subtraction.
        for _ in range(64):
            value = rng.randrange(-(1 << 63), 1 << 63)
            initial = rng.randrange(-(1 << 63), 1 << 63)
            remainder, result = value, initial
            for exponent in range(2*(steps-1), -1, -2):
                bit = 1 << exponent
                candidate = signed64(result+bit)
                take = remainder >= candidate
                remainder = signed64(remainder-candidate) if take else remainder
                result = signed64((result >> 1)+bit) if take else result >> 1
            cases.append((value, initial, result))
        for i, (value, initial, expected) in enumerate(cases):
            main += [f'%x{i} = arith.constant {value} : i64',
                     f'%e{i} = arith.constant {expected} : i64',
                     f'%initial{i} = arith.constant {initial} : i64',
                     f'%got{i} = func.call @root(%x{i}, %initial{i}) : (i64, i64) -> i64',
                     f'%bad{i} = arith.cmpi ne, %got{i}, %e{i} : i64',
                     f'%wide{i} = arith.extui %bad{i} : i1 to i64',
                     f'%sum{i} = arith.addi {accumulator}, %wide{i} : i64']
            accumulator = f'%sum{i}'
        main += [f'return {accumulator} : i64', '}']
        source = fixture(steps=steps).rsplit('}', 1)[0]+'\n'+'\n'.join(main)+'\n}'
        executor = SequentialScratchTests()
        self.assertEqual(executor.execute(source), 0)
        self.assertEqual(executor.execute(self.lower(source)), 0)


if __name__ == '__main__':
    unittest.main()
