"""Optional native sharing cleanup composes the existing dead-cell pass."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

class ComponentCleanupTests(unittest.TestCase):
    def test_cleanup_matches_explicit_native_pass_composition(self):
        tool=Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'fixture.futil'
            source.write_text('''import "primitives/core.futil";
component main() -> () {
 cells { a=std_reg(1); b=std_reg(1); @protected(1) keep=std_reg(1); }
 wires {
 group first { a.in=1'd0; a.write_en=1'd1; first[done]=a.done; }
 group second { b.in=1'd1; b.write_en=1'd1; second[done]=b.done; }
 }
 control { seq { first; second; } }
}
component extra() -> () { cells { unused=std_reg(1); } wires {} control {} }
''')
            def run(extra):
                result=subprocess.run([str(tool),str(source),'-p','cell-share',
                    '-x','cell-share:bounds=1,-1,-1','-b','calyx','--log','info',
                    '-l',str(tool.parent.parent/'share/calyx'),*extra],
                    capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                return result
            canonical=run(['-p','dead-cell-removal'])
            candidate=run(['-x','cell-share:retire-dead-cells=true'])
            self.assertIn('cell-share:main:cleanup:end',candidate.stderr)
            self.assertIn('cell-share:main:allocator-reclamation', candidate.stderr)
            components = re.findall(r'cell-share:([^: ]+):liveness:start', candidate.stderr)
            self.assertEqual(set(components), {'main', 'extra'})
            for index, name in enumerate(components):
                start = candidate.stderr.index(f'cell-share:{name}:liveness:start')
                end = candidate.stderr.index(f'cell-share:{name}:cleanup:end')
                self.assertLess(start, end)
                if index + 1 < len(components):
                    self.assertLess(end, candidate.stderr.index(
                        f'cell-share:{components[index + 1]}:liveness:start'))
            self.assertEqual(candidate.stdout,canonical.stdout)
            self.assertIn('keep = std_reg',candidate.stdout)
            original=run([])
            self.assertNotIn('cell-share:main:cleanup:start',original.stderr)
            self.assertGreater(original.stdout.count('= std_reg'),candidate.stdout.count('= std_reg'))
