"""Native per-component serialization preserves the complete hierarchy."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path

SOURCE='''import "primitives/core.futil";
component helper(x: 8) -> (y: 8) {
 cells { add = std_add(8); }
 wires { add.left = x; add.right = 8'd1; y = add.out; done = 1'd1; }
 control {}
}
component main(x: 8) -> (y: 8) {
 cells { worker = helper(); }
 wires {}
 control { invoke worker(x=x)(y=y); }
}
'''

class NativeComponentFileTests(unittest.TestCase):
    def run_tool(self,root,*,split=False,nested=False,extra=()):
        tool=Path(os.environ['LLM2FPGA_CALYX_TOOL']);source=root/'input.futil';source.write_text(SOURCE)
        options=['--synthesis']+(['--nested'] if nested else [])
        if split:options+=['-x',f'verilog-component-dir={root}/components']
        return subprocess.run([str(tool),str(source),'-l',str(tool.parent.parent/'share/calyx'),
            '-b','verilog',*options,*extra,'-o',str(root/('library.sv' if split else 'whole.sv'))],capture_output=True,text=True,timeout=60)
    def test_concatenation_is_byte_identical_for_both_native_guard_modes(self):
        for nested in (False,True):
            with self.subTest(nested=nested),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                whole=self.run_tool(root,nested=nested);self.assertEqual(whole.returncode,0,whole.stderr)
                split=self.run_tool(root,split=True,nested=nested);self.assertEqual(split.returncode,0,split.stderr)
                manifest=json.loads((root/'components/manifest.json').read_text())
                self.assertEqual(manifest['entrypoint'],'main')
                self.assertEqual({row['component'] for row in manifest['components']},{'main','helper'})
                merged=(root/'library.sv').read_bytes()
                for row in manifest['components']:merged+=(root/'components'/row['file']).read_bytes()
                self.assertEqual(merged,(root/'whole.sv').read_bytes())
    def test_existing_output_directory_is_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);directory=root/'components';directory.mkdir();marker=directory/'keep';marker.write_text('original')
            result=self.run_tool(root,split=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(marker.read_text(),'original')
            self.assertFalse((directory/'manifest.json').exists())

    def test_invalid_backend_options_fail_explicitly(self):
        for options in [('-x','verilog-component-dir='),
                        ('-x','verilog-component-dir=one','-x','verilog-component-dir=two')]:
            with self.subTest(options=options),tempfile.TemporaryDirectory() as temporary:
                result=self.run_tool(Path(temporary),extra=options)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('invalid verilog-component-dir',result.stderr)
