"""Select runtime memories in typed IR before native Calyx externalization."""
import os
import subprocess
import tempfile
from pathlib import Path
import unittest


def fixture(include_reference=True):
    cells = []
    for name, role, depth, ref in (
            ('a', 'prompt', 4, False), ('b', 'generated_tokens', 16, False),
            ('c', 'last_logits', 3, False), ('d', 'prompt', 4, True)):
        if ref and not include_reference:
            continue
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

    def test_only_physical_runtime_memories_are_externalized(self):
        result = self.expose(fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('external = true'), 3)
        reference = next(line for line in result.stdout.splitlines()
                         if 'calyx.primitive @d ' in line)
        self.assertNotIn('external', reference)
        again = self.expose(result.stdout)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, result.stdout)

    def test_public_cell_names_follow_roles(self):
        result = self.expose(fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        for role in ('prompt', 'generated_tokens', 'last_logits'):
            self.assertIn(f'calyx.primitive @{role} ', result.stdout)

    def test_reserved_runtime_name_collision_is_rejected(self):
        result = self.expose(fixture().replace('@d ', '@prompt '))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime interface name collision', result.stderr)

    def test_invocation_reference_tracks_runtime_symbol(self):
        source = fixture().replace('calyx.wires {',
            '%worker.clk, %worker.reset, %worker.go, %worker.done = '
            'calyx.instance @worker of @reader : i1, i1, i1, i1\ncalyx.wires {')
        source = source.replace('calyx.control {}',
            'calyx.control { calyx.invoke @worker[weights = a]() -> () }')
        reader = '''calyx.component @reader(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
          %weights.addr, %weights.read = calyx.primitive @weights of @memory
            {llm2fpga.ref = true} : i2, i64
          calyx.wires { calyx.assign %done = %go : i1 }
          calyx.control {}
        }'''
        source = source.rsplit('}', 1)[0] + reader + '}'
        result = self.expose(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('weights = prompt', result.stdout)

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

    def test_native_externalize_exports_named_runtime_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primitive = root / 'memory.sv'
            primitive.write_text('module memory(input [1:0] addr, output [63:0] read); assign read = {62\'d0, addr}; endmodule\n')
            source = fixture(False).replace('out read: i64)',
                f'out read: i64) attributes {{filename = "{primitive}"}}')
            exposed = self.expose(source)
            self.assertEqual(exposed.returncode, 0, exposed.stderr)
            translated = subprocess.run([os.environ['LLM2FPGA_CALYX_TRANSLATE_TOOL'],
                '--export-calyx'], input=exposed.stdout, text=True,
                capture_output=True, timeout=30)
            self.assertEqual(translated.returncode, 0, translated.stderr)
            program = root / 'kernel.futil'
            program.write_text(translated.stdout)
            calyx = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
            result = subprocess.run([str(calyx), str(program), '-p', 'external',
                '-b', 'verilog', '-l', str(calyx.parent.parent / 'share/calyx')],
                text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for role in ('prompt', 'generated_tokens', 'last_logits'):
                self.assertIn(role + '_read', result.stdout)
                self.assertIn(role + '_addr', result.stdout)
