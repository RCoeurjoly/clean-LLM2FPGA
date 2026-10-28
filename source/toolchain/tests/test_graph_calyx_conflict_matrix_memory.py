"""Dense register conflicts must not consume byte-per-pair matrix storage."""
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest


class ConflictMatrixMemoryTests(unittest.TestCase):
    def test_dense_register_conflicts_under_memory_limit(self):
        tool = Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        count = 18000
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'conflicts.futil'
            with source.open('w') as output:
                output.write('import "primitives/core.futil";\ncomponent main() -> () { cells {\n')
                for index in range(count):
                    output.write(f'r{index}=std_reg(1);\n')
                output.write('} wires { group simultaneous {\n')
                for index in range(count):
                    output.write(f"r{index}.in=r{index}.out; r{index}.write_en=1'd1;\n")
                output.write("simultaneous[done]=1'd1; } } control { simultaneous; } }\n")

            def memory_limit():
                limit = 256 * 1024**2
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            result = subprocess.run([str(tool), str(source), '-p', 'cell-share',
                '-x', 'cell-share:bounds=1,-1,-1', '-b', 'calyx', '-o', os.devnull,
                '--log', 'info', '-l', str(tool.parent.parent / 'share/calyx')],
                preexec_fn=memory_limit, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cell-share:main:conflicts:type:std_reg_WIDTH_1:nodes:18000', result.stderr)
        self.assertIn('cell-share:main:coloring:end', result.stderr)
