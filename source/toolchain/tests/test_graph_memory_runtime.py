"""Resolve semantic runtime buffers through typed invocation references."""
import os
import subprocess
import unittest
from test_graph_memory_binding import MULTI_SOURCE


def fixture():
    # Authored IR fixture: three independent memories in the same invocation.
    source = MULTI_SOURCE
    physical = next(line for line in source.splitlines() if 'calyx.seq_mem @m ' in line)
    reference = next(line for line in source.splitlines() if 'calyx.seq_mem @weights ' in line)
    source = source.replace(physical, '\n'.join(
        physical.replace('%m.', f'%m{i}.').replace('@m ', f'@m{i} ')
        for i in range(3)))
    source = source.replace(reference, '\n'.join(
        reference.replace('%weights.', f'%weights{i}.').replace('@weights ', f'@weights{i} ')
        .replace(' [2] :', f' [2] {{llm2fpga.runtime_port = "{role}"}} :')
        for i, role in enumerate(('prompt', 'generated_tokens', 'last_logits'))))
    source = source.replace('[weights = m]', '[weights0 = m0, weights1 = m1, weights2 = m2]')
    source = source.replace('%weights.', '%weights0.')
    return source


class RuntimeMemoryTests(unittest.TestCase):
    def resolve(self, source):
        return subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--llm2fpga-resolve-runtime-memories'], input=source,
            text=True, capture_output=True, timeout=30)


    def test_missing_role_is_rejected(self):
        result = self.resolve(fixture().replace(
            'llm2fpga.runtime_port = "last_logits"', 'unused_role = "last_logits"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing runtime role', result.stderr)

    def test_unknown_role_is_rejected(self):
        result = self.resolve(fixture().replace(
            'llm2fpga.runtime_port = "prompt"', 'llm2fpga.runtime_port = "unknown"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown runtime role', result.stderr)

    def test_conflicting_roles_on_one_physical_memory_are_rejected(self):
        result = self.resolve(fixture().replace('weights1 = m1', 'weights1 = m0'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('conflicting runtime roles', result.stderr)
