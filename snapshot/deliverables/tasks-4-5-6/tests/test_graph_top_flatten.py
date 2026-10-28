"""Top-only native flattening must retain nested signed combinational behavior."""
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

SOURCE='''module leaf(input signed [7:0] a,b, output [7:0] y);
assign y=(a*b)+(a>>>3); endmodule
module middle(input [7:0] a,b,output [7:0] y);
leaf child(a,b,y); endmodule
module main(input [7:0] a,b,output [7:0] y);
middle child(a,b,y); endmodule
'''


class TopFlattenTests(unittest.TestCase):
    def passes(self,top):
        name='compiler.yosys_pipeline'
        self.assertIsNotNone(importlib.util.find_spec(name),'bounded native flatten policy missing')
        module=__import__(name,fromlist=['top_flatten_passes'])
        return module.top_flatten_passes(top)

    def test_native_nested_signed_logic_is_equivalent(self):
        passes=self.passes('main')
        yosys=shutil.which('yosys')
        self.assertIsNotNone(yosys,'native Yosys required')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'source.sv').write_text(SOURCE)
            commands=['read_verilog -sv source.sv','hierarchy -check -top main','proc',
                      'design -save raw','flatten -noscopeinfo','rename main gold','design -stash gold',
                      'design -load raw',*passes,'rename main gate','design -stash gate',
                      'design -copy-from gold -as gold gold','design -copy-from gate -as gate gate',
                      'equiv_make gold gate equiv','hierarchy -check -top equiv',
                      'equiv_simple','equiv_status -assert']
            result=subprocess.run([yosys,'-p','; '.join(commands)],cwd=root,
                text=True,capture_output=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stderr+'\n'+result.stdout[-3000:])
            self.assertIn('Equivalence successfully proven',result.stdout)

    def test_top_selection_cannot_inject_another_pass(self):
        with self.assertRaisesRegex(ValueError,'symbol'):
            self.passes('main; clean')
