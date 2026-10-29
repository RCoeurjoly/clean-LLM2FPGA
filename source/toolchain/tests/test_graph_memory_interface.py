"""Select runtime memories in typed IR before native Calyx externalization."""
import os
import subprocess
import unittest


def fixture():
    cells = []
    for name, role, depth, ref in (
            ('a', 'prompt', 4, False), ('b', 'generated_tokens', 16, False),
            ('c', 'last_logits', 3, False), ('d', 'prompt', 4, True)):
        cells.append(f'''%{name}.addr, %{name}.read = calyx.primitive @{name} of @memory
          {{llm2fpga.runtime_port = "{role}", llm2fpga.ref = {str(ref).lower()},
            llm2fpga.binding = {{width = 64 : i64, depth = {depth} : i64,
              writable = true, read_latency = 2 : i64}}}} : i2, i64''')
    return '''module attributes {calyx.entrypoint = "main"} {
      hw.module.extern @memory(in %addr: i2, out read: i64)
      calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
      ''' + '\n'.join(cells) + '''
        calyx.wires { calyx.assign %done = %go : i1 }
        calyx.control {}
      } {toplevel}
    }'''


class RuntimeInterfaceTests(unittest.TestCase):
    def expose(self, source):
        return subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],
            '--verify-each', '--llm2fpga-expose-runtime-memories'], input=source,
            text=True, capture_output=True, timeout=30)


    def test_reserved_runtime_name_collision_is_rejected(self):
        result = self.expose(fixture().replace('@d ', '@prompt '))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime interface name collision', result.stderr)


    def test_wrong_prompt_geometry_is_rejected(self):
        result = self.expose(fixture().replace('depth = 4 : i64', 'depth = 5 : i64'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime memory geometry', result.stderr)

    def test_missing_role_is_rejected(self):
        result = self.expose(fixture().replace('"last_logits"', '"prompt"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing physical runtime role: last_logits', result.stderr)

    def test_duplicate_role_is_rejected(self):
        source = fixture().replace('"last_logits"', '"prompt"').replace(
            'depth = 3 : i64', 'depth = 4 : i64')
        result = self.expose(source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('duplicate physical runtime role', result.stderr)
